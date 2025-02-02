import os
import jax
import time
import wandb
import numpy as np
from typing import Any
import jax.numpy as jnp

import wandb
import hydra
import optax
import gymnax
from flax import struct
import flax.linen as nn
from omegaconf import OmegaConf
from jaxmarl.wrappers.baselines import load_params
from flax.core.frozen_dict import freeze, unfreeze
from flax.training.train_state import TrainState
from gymnax.wrappers.purerl import FlattenObservationWrapper, LogWrapper

class CNN(nn.Module):
    norm_type: str = "layer_norm"

    @nn.compact
    def __call__(self, x: jnp.ndarray, train: bool):
        if self.norm_type == "layer_norm":
            normalize = lambda x: nn.LayerNorm()(x)
        elif self.norm_type == "batch_norm":
            normalize = lambda x: nn.BatchNorm(use_running_average=not train)(x)
        else:
            normalize = lambda x: x

        x = nn.Conv(
            16,
            kernel_size=(3, 3),
            strides=1,
            padding="VALID",
            kernel_init=nn.initializers.he_normal(),
        )(x)
        x = normalize(x)
        x = nn.relu(x)
        x = x.reshape((x.shape[0], -1))
        x = nn.Dense(128, kernel_init=nn.initializers.he_normal())(x)
        x = normalize(x)
        x = nn.relu(x)
        return x

class QNetwork(nn.Module):
    action_dim: int
    norm_type: str = "layer_norm"
    norm_input: bool = False

    @nn.compact
    def __call__(self, x: jnp.ndarray, train: bool):
        if self.norm_input:
            x = nn.BatchNorm(use_running_average=not train)(x)
        else:
            x = x / 255.0
        x = CNN(norm_type=self.norm_type)(x, train)
        x = nn.Dense(self.action_dim)(x)
        return x

@struct.dataclass
class Transition:
    obs: jnp.ndarray
    action: jnp.ndarray
    reward: jnp.ndarray
    done: jnp.ndarray
    next_obs: jnp.ndarray
    q_val: jnp.ndarray

class CustomTrainState(TrainState):
    batch_stats: Any
    timesteps: int = 0
    n_updates: int = 0
    grad_steps: int = 0

def load_model_parameters(file_path):
    """
    Load the model parameters and batch_stats from a safetensors file
    """
    loaded = load_params(file_path)
    return freeze(loaded), freeze({})

def reinit_layers_if_needed(network, 
                            loaded_params, 
                            rng_for_init, 
                            input_shape, 
                            force_reinit_names=()):
    """
    Create a fresh set of parameters for the `network`, then do a 
    shape-based (or name-based) merge with `loaded_params`. 
    Layers that have changed shape or appear in `force_reinit_names`
    get reinitialized; all others use the old weights.
    """
    # 1. Create a "fresh" param set for the new env shape
    dummy_input = jnp.zeros((1, *input_shape), dtype=jnp.float32)
    new_vars = network.init(rng_for_init, dummy_input, train=False)
    new_params = unfreeze(new_vars["params"])

    # 2. Make a mutable copy of loaded_params
    old_params = unfreeze(loaded_params)

    # 3. Recursively merge
    def recursively_merge(old, new, path=()):
        # path is used if you want to force-reinit by param name
        if isinstance(old, dict) and isinstance(new, dict):
            merged = {}
            for k in new.keys():
                merged[k] = recursively_merge(old.get(k, {}), new[k], path + (k,))
            return merged
        else:
            # We have reached an actual array/tensor param
            param_name = ".".join(path)
            # shape-based or name-based check
            if param_name in force_reinit_names:
                # Force reinit
                return new
            elif old.shape == new.shape:
                # Keep old param
                return old
            else:
                # shape mismatch => reinit
                return new

    merged_params = recursively_merge(old_params, new_params)
    return freeze(merged_params), freeze(new_vars.get("batch_stats", {}))

def make_train(config):

    env_name_learning = config["ENV_NAME_DEPLOY"]
    config["NUM_UPDATES"] = (
        config["TOTAL_TIMESTEPS"] // config["NUM_STEPS"] // config["NUM_ENVS"]
    )

    config["NUM_UPDATES_DECAY"] = (
        config["TOTAL_TIMESTEPS_DECAY"] // config["NUM_STEPS"] // config["NUM_ENVS"]
    )

    assert (config["NUM_STEPS"] * config["NUM_ENVS"]) % config["NUM_MINIBATCHES"] == 0, "NUM_MINIBATCHES must divide NUM_STEPS*NUM_ENVS"

    env, env_params = gymnax.make(env_name_learning)
    env = LogWrapper(env)
    config["TEST_NUM_STEPS"] = env_params.max_steps_in_episode

    vmap_reset = lambda n_envs: lambda rng: jax.vmap(env.reset, in_axes=(0, None))(jax.random.split(rng, n_envs), env_params)
    vmap_step = lambda n_envs: lambda rng, env_state, action: jax.vmap(
        env.step, in_axes=(0, 0, 0, None)
    )(jax.random.split(rng, n_envs), env_state, action, env_params)

    def eps_greedy_exploration(rng, q_vals, eps):
        rng_a, rng_e = jax.random.split(rng)
        greedy_actions = jnp.argmax(q_vals, axis=-1)
        chosen_actions = jnp.where(
            jax.random.uniform(rng_e, greedy_actions.shape) < eps,
            jax.random.randint(rng_a, shape=greedy_actions.shape, minval=0, maxval=q_vals.shape[-1]),
            greedy_actions,
        )
        return chosen_actions

    def train(rng):
        original_rng = rng[0]

        eps_scheduler = optax.linear_schedule(
            config["EPS_START"],
            config["EPS_FINISH"],
            (config["EPS_DECAY"]) * config["NUM_UPDATES_DECAY"],
        )

        lr_scheduler = optax.linear_schedule(
            init_value=config["LR"],
            end_value=1e-20,
            transition_steps=(config["NUM_UPDATES_DECAY"])
            * config["NUM_MINIBATCHES"]
            * config["NUM_EPOCHS"],
        )
        lr = lr_scheduler if config.get("LR_LINEAR_DECAY", False) else config["LR"]

        network = QNetwork(
            action_dim=gymnax.make(env_name_learning)[0].action_space(env_params).n,
            norm_type=config["NORM_TYPE"],
            norm_input=config.get("NORM_INPUT", False),
        )

        def create_agent(rng):
            init_x = jnp.zeros((1, *env.observation_space(env_params).shape))
            network_variables = network.init(rng, init_x, train=False)
            tx = optax.chain(
                optax.clip_by_global_norm(config["MAX_GRAD_NORM"]),
                optax.radam(learning_rate=lr),
            )

            train_state = CustomTrainState.create(
                apply_fn=network.apply,
                params=freeze(network_variables["params"]),  # Ensure params are frozen here
                batch_stats=network_variables.get("batch_stats", {}),
                tx=tx,
            )
            return train_state

        rng, _rng = jax.random.split(rng)
        train_state = create_agent(rng)

        # Load pretrained parameters if specified
        if "LOAD_PATH" in config and config["LOAD_PATH"] is not None:
            load_path = config["LOAD_PATH"]
            loaded_params, _ = load_model_parameters(load_path)

            # Figure out new input shape and new output_dim
            input_shape = env.observation_space(env_params).shape
            output_dim = env.action_space(env_params).n

            # Decide which layers to force reinit
            # (e.g. the first conv if # channels changed, or the last dense for new # of actions)
            force_reinit = []

            # Example: check if CNN_0 Conv_0 kernel last dimension matches the new input channels
            if (config['REINIT_INPUT']):
                old_input_channels = loaded_params["CNN_0"]["Conv_0"]["kernel"].shape[2]
                new_input_channels = input_shape[-1]
                if old_input_channels != new_input_channels:
                    force_reinit.append("CNN_0.Conv_0.bias")
                    force_reinit.append("CNN_0.Conv_0.kernel")
            
            # Example: always reinit last Dense if action_dim changed 
            if (config['REINIT_OUTPUT']):
                old_outdim = loaded_params["Dense_0"]["bias"].shape[0]
                if old_outdim != output_dim:
                    force_reinit.append("Dense_0.bias")
                    force_reinit.append("Dense_0.kernel")

            # Reinit necessary layers
            # create your network again with the new environment's dims
            network = QNetwork(
                action_dim=output_dim,
                norm_type=config["NORM_TYPE"],
                norm_input=config.get("NORM_INPUT", False),
            )

            rng_for_init = jax.random.PRNGKey(999)  # or any seed you like
            new_params, new_batch_stats = reinit_layers_if_needed(
                network,
                loaded_params,
                rng_for_init,
                input_shape,
                force_reinit_names=tuple(force_reinit),
            )

            # Now update your train state
            train_state = train_state.replace(params=new_params,
                                            batch_stats=new_batch_stats)

        def get_test_metrics(train_state, rng):
            if not config.get("TEST_DURING_TRAINING", False):
                return None

            def _env_step(carry, _):
                env_state, last_obs, rng = carry
                rng, _rng = jax.random.split(rng)
                q_vals = network.apply(
                    {
                        "params": train_state.params,
                        "batch_stats": train_state.batch_stats,
                    },
                    last_obs,
                    train=False,
                )
                eps = jnp.full(config["TEST_NUM_ENVS"], config["EPS_TEST"])
                action = jax.vmap(eps_greedy_exploration)(
                    jax.random.split(_rng, config["TEST_NUM_ENVS"]), q_vals, eps
                )
                new_obs, new_env_state, reward, done, info = vmap_step(
                    config["TEST_NUM_ENVS"]
                )(_rng, env_state, action)
                return (new_env_state, new_obs, rng), info

            rng, _rng = jax.random.split(rng)
            init_obs, env_state = vmap_reset(config["TEST_NUM_ENVS"])(_rng)

            _, infos = jax.lax.scan(
                _env_step, (env_state, init_obs, _rng), None, config["TEST_NUM_STEPS"]
            )
            done_infos = jax.tree_map(
                lambda x: jnp.nanmean(
                    jnp.where(
                        infos["returned_episode"],
                        x,
                        jnp.nan,
                    )
                ),
                infos,
            )
            return done_infos

        def _update_step(runner_state, unused):

            train_state, expl_state, test_metrics, rng = runner_state

            # SAMPLE PHASE
            def _step_env(carry, _):
                last_obs, env_state, rng = carry
                rng, rng_a, rng_s = jax.random.split(rng, 3)
                q_vals = network.apply(
                    {
                        "params": train_state.params,
                        "batch_stats": train_state.batch_stats,
                    },
                    last_obs,
                    train=False,
                )

                _rngs = jax.random.split(rng_a, config["NUM_ENVS"])
                eps = jnp.full(config["NUM_ENVS"], eps_scheduler(train_state.n_updates))
                new_action = jax.vmap(eps_greedy_exploration)(_rngs, q_vals, eps)

                new_obs, new_env_state, reward, new_done, info = vmap_step(
                    config["NUM_ENVS"]
                )(rng_s, env_state, new_action)

                transition = Transition(
                    obs=last_obs,
                    action=new_action,
                    reward=config.get("REW_SCALE", 1)*reward,
                    done=new_done,
                    next_obs=new_obs,
                    q_val=q_vals,
                )
                return (new_obs, new_env_state, rng), (transition, info)

            rng, _rng = jax.random.split(rng)
            (*expl_state, rng), (transitions, infos) = jax.lax.scan(
                _step_env,
                (*expl_state, _rng),
                None,
                config["NUM_STEPS"],
            )
            expl_state = tuple(expl_state)

            train_state = train_state.replace(
                timesteps=train_state.timesteps + config["NUM_STEPS"] * config["NUM_ENVS"]
            )

            last_q = network.apply(
                {
                    "params": train_state.params,
                    "batch_stats": train_state.batch_stats,
                },
                transitions.next_obs[-1],
                train=False,
            )
            last_q = jnp.max(last_q, axis=-1)

            def _get_target(lambda_returns_and_next_q, transition):
                lambda_returns, next_q = lambda_returns_and_next_q
                target_bootstrap = (
                    transition.reward + config["GAMMA"] * (1 - transition.done) * next_q
                )
                delta = lambda_returns - next_q
                lambda_returns = (
                    target_bootstrap + config["GAMMA"] * config["LAMBDA"] * delta
                )
                lambda_returns = (
                    1 - transition.done
                ) * lambda_returns + transition.done * transition.reward
                next_q = jnp.max(transition.q_val, axis=-1)
                return (lambda_returns, next_q), lambda_returns

            last_q = last_q * (1 - transitions.done[-1])
            lambda_returns = transitions.reward[-1] + config["GAMMA"] * last_q
            _, targets = jax.lax.scan(
                _get_target,
                (lambda_returns, last_q),
                jax.tree_map(lambda x: x[:-1], transitions),
                reverse=True,
            )
            lambda_targets = jnp.concatenate((targets, lambda_returns[np.newaxis]))

            def _learn_epoch(carry, _):
                train_state, rng = carry

                def _learn_phase(carry, minibatch_and_target):
                    train_state, rng = carry
                    minibatch, target = minibatch_and_target

                    def _loss_fn(params):
                        q_vals, updates = network.apply(
                            {"params": params, "batch_stats": train_state.batch_stats},
                            minibatch.obs,
                            train=True,
                            mutable=["batch_stats"],
                        )
                        chosen_action_qvals = jnp.take_along_axis(
                            q_vals,
                            jnp.expand_dims(minibatch.action, axis=-1),
                            axis=-1,
                        ).squeeze(axis=-1)
                        loss = 0.5 * jnp.square(chosen_action_qvals - target).mean()
                        return loss, (updates, chosen_action_qvals)

                    # Unfreeze/Freeze as usual for param updates
                    mutable_params = unfreeze(train_state.params)
                    (loss, (updates, qvals)), grads = jax.value_and_grad(_loss_fn, has_aux=True)(mutable_params)
                    train_state = train_state.apply_gradients(grads=freeze(grads))

                    # IMPORTANT: freeze the updated batch_stats so it remains a FrozenDict
                    new_batch_stats = freeze(updates["batch_stats"])
                    train_state = train_state.replace(batch_stats=new_batch_stats)

                    return (train_state, rng), (loss, qvals)

                def preprocess_transition(x, rng):
                    x = x.reshape(
                        -1, *x.shape[2:]
                    )
                    x = jax.random.permutation(rng, x)
                    x = x.reshape(
                        config["NUM_MINIBATCHES"], -1, *x.shape[1:]
                    )
                    return x

                rng, _rng = jax.random.split(rng)
                minibatches = jax.tree_util.tree_map(
                    lambda x: preprocess_transition(x, _rng), transitions
                )
                targets = jax.tree_map(
                    lambda x: preprocess_transition(x, _rng), lambda_targets
                )

                rng, _rng = jax.random.split(rng)
                (train_state, rng), (loss, qvals) = jax.lax.scan(
                    _learn_phase, (train_state, rng), (minibatches, targets)
                )

                return (train_state, rng), (loss, qvals)

            rng, _rng = jax.random.split(rng)
            (train_state, rng), (loss, qvals) = jax.lax.scan(
                _learn_epoch, (train_state, rng), None, config["NUM_EPOCHS"]
            )

            train_state = train_state.replace(n_updates=train_state.n_updates + 1)
            metrics = {
                "env_step": train_state.timesteps,
                "update_steps": train_state.n_updates,
                "env_frame": train_state.timesteps * env.observation_space(env_params).shape[-1],
                "grad_steps": train_state.grad_steps,
                "td_loss": loss.mean(),
                "qvals": qvals.mean(),
            }
            metrics.update({k: v.mean() for k, v in infos.items()})

            if config.get("TEST_DURING_TRAINING", False):
                rng, _rng = jax.random.split(rng)
                test_metrics = jax.lax.cond(
                    train_state.n_updates
                    % int(config["NUM_UPDATES"] * config["TEST_INTERVAL"])
                    == 0,
                    lambda _: get_test_metrics(train_state, _rng),
                    lambda _: test_metrics,
                    operand=None,
                )
                metrics.update({f"test_{k}": v for k, v in (test_metrics or {}).items()})

            if config["WANDB_MODE"] != "disabled":
                def callback(metrics, original_rng):
                    if config.get("WANDB_LOG_ALL_SEEDS", False):
                        metrics.update(
                            {
                                f"rng{int(original_rng)}/{k}": v
                                for k, v in metrics.items()
                            }
                        )
                    wandb.log(metrics, step=metrics["update_steps"])

                jax.debug.callback(callback, metrics, original_rng)

            runner_state = (train_state, tuple(expl_state), test_metrics, rng)
            return runner_state, metrics

        rng, _rng = jax.random.split(rng)
        test_metrics = get_test_metrics(train_state, _rng)

        rng, _rng = jax.random.split(rng)
        expl_state = vmap_reset(config["NUM_ENVS"])(_rng)

        rng, _rng = jax.random.split(rng)
        runner_state = (train_state, expl_state, test_metrics, _rng)

        runner_state, metrics = jax.lax.scan(
            _update_step, runner_state, None, config["NUM_UPDATES"]
        )

        return {"runner_state": runner_state, "metrics": metrics}

    return train

def download_csv_from_wandb(run_id, project_name, entity_name, save_dir):
    """
    Download the CSV files for a specific WandB run.
    """
    api = wandb.Api()

    # Fetch the run
    run = api.run(f"{entity_name}/{project_name}/{run_id}")
    history = run.history(pandas=False)

    # Initialize data holders
    env_steps = []
    returned_returns = []
    test_returned_returns = []

    # Extract data from WandB history
    for entry in history:
        if "env_step" in entry:
            if "returned_episode_returns" in entry:
                env_steps.append(entry["env_step"])
                returned_returns.append(entry["returned_episode_returns"])
            if "test_returned_episode_returns" in entry:
                test_returned_returns.append(entry["test_returned_episode_returns"])

    # Save the data to CSV files
    timestamp = int(time.time())
    os.makedirs(save_dir, exist_ok=True)

    # Save returned_episode_returns vs env_step
    csv_path_returns = os.path.join(save_dir, f"{timestamp}.csv")
    with open(csv_path_returns, "w") as f:
        f.write("env_step,returned_episode_returns\n")
        for step, returns in zip(env_steps, returned_returns):
            f.write(f"{step},{returns}\n")
    print(f"CSV for returned_episode_returns saved to: {csv_path_returns}")

    # Save test_returned_episode_returns vs env_step
    csv_path_test_returns = os.path.join(save_dir, f"{timestamp}_NN.csv")
    with open(csv_path_test_returns, "w") as f:
        f.write("env_step,test_returned_episode_returns\n")
        for step, test_returns in zip(env_steps, test_returned_returns):
            f.write(f"{step},{test_returns}\n")
    print(f"CSV for test_returned_episode_returns saved to: {csv_path_test_returns}")

def single_run(config):

    config = {**config, **config["alg"]}

    alg_name = config.get("ALG_NAME", "pqn")
    env_name_deploy = config["ENV_NAME_DEPLOY"]
    env_name_learn = config["ENV_NAME_LEARNING"]

    wandb.init(
        entity=config["ENTITY"],
        project=config["PROJECT"],
        tags=[
            alg_name.upper(),
            env_name_deploy.upper(),
            f"jax_{jax.__version__}",
        ],
        name=f'{env_name_deploy}_Transfer_From_{config["ENV_NAME_LEARNING"]}',
        config=config,
        mode=config["WANDB_MODE"],
    )
    
    rng = jax.random.PRNGKey(config["SEED"])

    run_id = wandb.run.id

    t0 = time.time()
    rngs = jax.random.split(rng, config["NUM_SEEDS"])
    train_vjit = jax.jit(jax.vmap(make_train(config)))
    outs = jax.block_until_ready(train_vjit(rngs))
    print(f"Took {time.time()-t0} seconds to complete.")

    download_csv_from_wandb(run_id, config["PROJECT"], config["ENTITY"], f"visualization/Dataset/{env_name_deploy}/{env_name_deploy}_Transfer_From_{env_name_learn}")

@hydra.main(version_base=None, config_path="./config", config_name="config")
def main(config):
    config = OmegaConf.to_container(config)
    single_run(config)

if __name__ == "__main__":
    main()
