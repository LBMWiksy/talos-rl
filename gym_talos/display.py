from pathlib import Path

import yaml
from stable_baselines3 import SAC

from .envs.env_talos_deburring_her import EnvTalosDeburringHer

training_name = "2023-07-17_pf_train_2"
train_name = "_".join(training_name.split("_")[:-1])


log_dir = Path("logs_pf")
model_path = log_dir / training_name / "best_model.zip"
config_path = log_dir / training_name / f"{train_name}.yaml"
with config_path.open() as config_file:
    params = yaml.safe_load(config_file)

envDisplay = EnvTalosDeburringHer(
    params["robot_designer"],
    params["environment"],
    GUI=True,
)

model = SAC.load(model_path, env=envDisplay)

envDisplay.maxTime = 500

while True:
    obs, info = envDisplay.reset()
    done = False
    i = 0
    while not done:
        i += 1
        action, _ = model.predict(obs, deterministic=True)
        obs, reward, terminated, truncated, _ = envDisplay.step(action)
        if terminated or truncated or i > 500:
            done = True
