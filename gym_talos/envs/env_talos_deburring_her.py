import gymnasium as gym
import numpy as np
import collections
from gym_talos.simulator.bullet_Talos import TalosDeburringSimulator

from ..utils.modelLoader import TalosDesigner
from ..utils.create_target import TargetGoal


class EnvTalosDeburringHer(gym.Env):
    def __init__(self, params_designer, params_env, GUI=False) -> None:
        """Defines the EnvTalosDeburring class

        Defines an interface a robot designer to handle interactions with pinocchio,
        an interface to the simulator that will be used
        as well as usefull internal variables.

        Args:
            params_designer: kwargs for the robot designer
            params_env: kwargs for the environment
            GUI: set to true to activate display. Defaults to False.
        """
        self._init_parameters(params_env, GUI)
        if self.reward_type == "dense":
            self.compute_reward = self.compute_reward_dense
        elif self.reward_type == "sparse":
            self.compute_reward = self.compute_reward_sparse
        # Robot Designer
        self.pinWrapper = TalosDesigner(
            URDF=params_designer["URDF"],
            SRDF=params_designer["SRDF"],
            toolPosition=params_designer["toolPosition"],
            controlledJoints=params_designer["controlledJoints"],
            set_gravity=True,
            dt=self.timeStepSimulation * self.numSimulationSteps,
        )

        self.rmodel = self.pinWrapper.rmodel
        # Simulator
        self.simulator = TalosDeburringSimulator(
            URDF=self.pinWrapper.URDF_path,
            rmodelComplete=self.pinWrapper.rmodelComplete,
            controlledJointsIDs=self.pinWrapper.controlledJointsID,
            randomInit=self.random_init_robot,
            enableGravity=True,
            enableGUI=GUI,
            dt=self.timeStepSimulation,
        )

        self.target = TargetGoal(params_env=params_env)

        # Penalization for truncation of torsos
        self.order_positions = self.simulator.dict_pos
        self.mat_dt_init = np.zeros(self.rmodel.nq)
        if self.weight_joints_to_init is not None:
            for key, value in self.weight_joints_to_init.items():
                self.mat_dt_init[self.order_positions[key]] = value
        self.mat_dt_init = np.diag(self.mat_dt_init)

        action_dimension = self.rmodel.nq
        observation_dimension = len(self.simulator.getRobotState())
        self._init_env_variables(action_dimension, observation_dimension)

    def _init_parameters(self, params_env, GUI):  # noqa: C901
        """Load environment parameters from provided dictionnary

        Args:
            params_env: kwargs for the environment
        """
        # Simumlation timings
        self.params_env = params_env
        self.timeStepSimulation = float(params_env["timeStepSimulation"])
        self.numSimulationSteps = params_env["numSimulationSteps"]

        self.normalizeObs = params_env["normalizeObs"]

        #   Stop conditions
        self.maxTime = params_env["maxTime"]

        #   Reward parameters
        self.weight_target = params_env["w_target_pos"]
        self.weight_command = params_env["w_control_reg"]
        self.weight_truncation = params_env["w_penalization_truncation"]
        self.GUI = GUI
        try:
            self.random_init_robot = params_env["randomInit"]
        except KeyError:
            self.random_init_robot = False
        try:
            self.limitPosScale = params_env["limitPosScale"]
        except KeyError:
            self.limitPosScale = 10
        try:
            self.limitVelScale = params_env["limitVelScale"]
        except KeyError:
            self.limitVelScale = 30
        try:
            self.torqueScaleCoeff = params_env["torqueScaleCoeff"]
        except KeyError:
            self.torqueScaleCoeff = 1
        try:
            self.lowerLimitPos = params_env["lowerLimitPos"]
        except KeyError:
            self.lowerLimitPos = [-0.5, -0.5, 0.9]
        try:
            self.upperLimitPos = params_env["upperLimitPos"]
        except KeyError:
            self.upperLimitPos = [0.5, 0.5, 1.5]
        try:
            self.threshold_success = params_env["thresholdSuccess"]
        except KeyError:
            self.threshold_success = 0.05
        try:
            self.weight_target_reached = params_env["w_target_reached"]
        except KeyError:
            self.weight_target_reached = 5
        try:
            self.weight_joints_to_init = params_env["w_joints_to_init"]
        except KeyError:
            self.weight_joints_to_init = None
        try:
            self.reward_type = params_env["rewardType"]
        except KeyError:
            self.reward_type = "dense"
        try:
            self.weight_alive = params_env["w_alive"]
        except KeyError:
            self.weight_alive = 1

    def _init_env_variables(self, action_dimension, observation_dimension):
        """Initialize internal variables of the environment

        Args:
            action_dimension: Dimension of the action space
            observation_dimension: Dimension of the observation space
        """
        self.timer = 0
        self.on_target = 0
        self.target.create_target()
        self.maxStep = int(
            self.maxTime / (self.timeStepSimulation * self.numSimulationSteps),
        )

        if self.normalizeObs:
            self._init_obsNormalizer()
            self._init_goalNormalizer()
            self._init_targetNormalizer()

        self.torqueScale = self.torqueScaleCoeff * np.array(self.rmodel.effortLimit)
        action_dim = action_dimension
        self.action_space = gym.spaces.Box(
            low=-1,
            high=1,
            shape=(action_dim,),
            dtype=np.float32,
        )

        # Having the required size of the observation space
        self.observation_space = gym.spaces.Dict()
        if self.normalizeObs:
            limit = 1
        else:
            limit = 10
        self.observation_space.spaces["observation"] = gym.spaces.Box(
            low=-limit,
            high=limit,
            shape=(observation_dimension,),
            dtype=np.float64,
        )
        self.observation_space.spaces["achieved_goal"] = gym.spaces.Box(
            low=-limit,
            high=limit,
            shape=(len(self.target.position_target),),
            dtype=np.float64,
        )
        self.observation_space.spaces["desired_goal"] = gym.spaces.Box(
            low=-limit,
            high=limit,
            shape=(len(self.target.position_target),),
            dtype=np.float64,
        )

    def close(self):
        """Properly shuts down the environment.

        Closes the simulator windows.
        """
        self.simulator.end()

    def reset(self, *, seed=None, options=None):
        """Reset the environment

        Brings the robot back to its half-sitting position

        Args:
            seed: seed that is used to initialize the environment's PRNG.
                Defaults to None.
            options: Additional information can be specified to reset the environment.
                Defaults to None.

        Returns:
            Observation of the initial state.
        """
        self.timer = 0
        self.on_target = 0
        if options is None:
            self.target.create_target()
        elif "target" in options.keys():
            self.target.position_target = options["target"]
        self.simulator.reset(self.target.position_target)  # Reset simulator
        x_measured = self.simulator.getRobotState()
        self.pinWrapper.update_reduced_model(x_measured, self.simulator.getRobotPos())
        infos = {
            "dst": np.linalg.norm(
                self.pinWrapper.get_end_effector_pos() - self.target.position_target,
            ),
            "tor": 0,
            "init": 0,
        }
        return self._getObservation(x_measured), infos

    def step(self, action):
        """Execute a step of the environment

        One step of the environment is numSimulationSteps of the simulator with the same
        command.
        The model of the robot is updated using the observation taken from the
        environment.
        The termination and condition are checked and the reward is computed.
        Args:
            action: Normalized action vector

        Returns:
            Formatted observations
            Reward
            Boolean indicating this rollout is done
        """
        self.timer += 1
        torques = self._scaleAction(action)

        for _ in range(self.numSimulationSteps):
            self.simulator.step(torques)
        x_measured = self.simulator.getRobotState()
        self.pinWrapper.update_reduced_model(x_measured, self.simulator.getRobotPos())
        # if self.GUI:
        #     # self.simulator.createBaseRobotVisual(
        #     # self.pinWrapper.get_end_effector_pos())
        #     pass
        self.rCoM = self.pinWrapper.get_CoM()
        ob = self._getObservation(x_measured)  # position velocity joint and goal
        truncated = self._checkTruncation(x_measured)
        reward, infos = self._reward(torques, ob, truncated)
        self.on_target += 1 if infos["on_target"] else 0
        terminated = self._checkTermination()
        if terminated or truncated:
            infos["is_success"] = self._checkSuccess()
        return ob, reward, terminated, truncated, infos

    def _getObservation(self, x_measured):
        """Formats observations

        Normalizes the observation obtained from the simulator if nomalizeObs = True

        Args:
            x_measured: observation array obtained from the simulator

        Returns:
            Fromated observations
        """
        final_obs = gym.spaces.Dict()
        if self.normalizeObs:
            observation = self._obsNormalizer(x_measured)
            achieved_goal = self._goalNormalizer(self.pinWrapper.get_end_effector_pos())
            desired_goal = self._targetNormalizer(self.target.position_target)
        else:
            observation = x_measured
            achieved_goal = self.pinWrapper.get_end_effector_pos()
            desired_goal = self.target.position_target
        final_obs.spaces["observation"] = np.array(observation)
        final_obs.spaces["achieved_goal"] = np.array(achieved_goal)
        final_obs.spaces["desired_goal"] = np.array(desired_goal)
        return collections.OrderedDict(final_obs)

    def _reward(self, torques, ob, truncated):
        """Compute step reward

        The reward is composed of:
            - A bonus when the environment is still alive (no constraint has been
              infriged)
            - A cost proportional to the norm of the torques
            - A cost proportional to the distance of the end-effector to the target

        Args:
            torques: torque vector
            ob: observation array obtained from the simulator
            terminated: termination bool
            truncated: truncation bool

        Returns:
            Scalar reward
        """
        len_to_init = np.sum(
            (self.simulator.qC0 - ob["observation"][: self.rmodel.nq]).T
            * self.mat_dt_init
            * (self.simulator.qC0 - ob["observation"][: self.rmodel.nq]),
        )
        dst = np.linalg.norm(ob["achieved_goal"] - ob["desired_goal"])

        bool_check = dst < self.threshold_success
        infos = {}
        infos["param_rew"] = np.array(
            [np.linalg.norm(torques), len_to_init, not truncated],
        )
        infos["tor"] = np.linalg.norm(torques)
        infos["init"] = len_to_init
        infos["dst"] = dst
        infos["on_target"] = bool_check
        ach_goal = np.empty((1, 3))
        des_goal = np.empty((1, 3))
        ach_goal[0, :] = ob["achieved_goal"]
        des_goal[0, :] = ob["desired_goal"]
        reward = float(
            self.compute_reward(
                achieved_goal=ach_goal,
                desired_goal=des_goal,
                info=np.array([infos]),
            ),
        )

        return reward, infos

    def _checkTermination(self):
        """Check the termination conditions.

        Environment is terminated when the task has been successfully carried out.
        In our case it means that maxTime has been reached.

        Args:
            x_measured: observation array obtained from the simulator

        Returns:
            True if the environment has been terminated, False otherwise
        """
        return self.timer > (self.maxStep - 1) or self.on_target > 30

    def _checkTruncation(self, x_measured):
        """Checks the truncation conditions.

        Environment is truncated when a constraint is infriged.
        There are two possible reasons for truncations:
         - Loss of balance of the robot:
            Rollout is stopped if position of CoM (or base currently) is under threshold
            No check is carried out if threshold is set to 0
         - Infrigement of the kinematic constraints of the robot
            Rollout is stopped if configuration exceeds model limits


        Args:
            x_measured: observation array obtained from the simulator

        Returns:
            True if the environment has been truncated, False otherwise.
        """
        # Balance
        truncation_balance = (self.rCoM < self.lowerLimitPos).any() or (
            self.rCoM > self.upperLimitPos
        ).any()
        # Limits
        truncation_limits_position = (
            x_measured[: self.rmodel.nq]
            > self.limitPosScale * self.rmodel.upperPositionLimit
        ).any() or (
            x_measured[: self.rmodel.nq]
            < self.limitPosScale * self.rmodel.lowerPositionLimit
        ).any()
        truncation_limits_speed = (
            np.abs(x_measured[-self.rmodel.nv :])
            > self.limitVelScale * self.rmodel.velocityLimit
        ).any()
        truncation_limits = truncation_limits_position or truncation_limits_speed
        return truncation_limits or truncation_balance

    def _checkSuccess(self):
        """Checks the success conditions.

        Environment is successful when the task has been successfully carried out.
        In our case it means that the end-effector is close enough to the target.

        Args:
            x_measured: observation array obtained from the simulator

        Returns:
            True if the environment has been successful, False otherwise.
        """
        return self.on_target > 30

    def _scaleAction(self, action):
        """Scales normalized actions to obtain robot torques

        Args:
            action: normalized action array

        Returns:
            torque array
        """
        return self.torqueScale * action

    def _init_obsNormalizer(self):
        """Initializes the observation normalizer using robot model limits"""
        self.lowerObsLim = np.concatenate(
            (
                self.rmodel.lowerPositionLimit,
                -self.rmodel.velocityLimit,
            ),
        )

        self.upperObsLim = np.concatenate(
            (
                self.rmodel.upperPositionLimit,
                self.rmodel.velocityLimit,
            ),
        )

        self.avgObs = (self.upperObsLim + self.lowerObsLim) / 2
        self.diffObs = self.upperObsLim - self.lowerObsLim

    def _init_goalNormalizer(self):
        """Initializes the goal normalizer using robot model limits"""
        self.lowerGoalLim = -3 * np.ones(3)
        self.upperGoalLim = 3 * np.ones(3)
        self.avgGoal = (self.upperGoalLim + self.lowerGoalLim) / 2
        self.diffGoal = self.upperGoalLim - self.lowerGoalLim

    def _init_targetNormalizer(self):
        """Initializes the target normalizer using robot model limits"""
        self.lowerGoalLim = -3 * np.ones(3)
        self.upperGoalLim = 3 * np.ones(3)
        self.avgGoal = (self.upperGoalLim + self.lowerGoalLim) / 2
        self.diffGoal = self.upperGoalLim - self.lowerGoalLim

    def _goalNormalizer(self, goal):
        """Normalizes the goal

        Args:
            goal: goal array

        Returns:
            normalized goal
        """
        return (goal - self.avgGoal) / self.diffGoal

    def _obsNormalizer(self, x_measured):
        """Normalizes the observation taken from the simulator

        Args:
            x_measured: observation array obtained from the simulator

        Returns:
            normalized observation
        """
        return (x_measured - self.avgObs) / self.diffObs

    def _targetNormalizer(self, target):
        """Normalizes the target

        Args:
            target: target array

        Returns:
            normalized target
        """
        return (target - self.avgGoal) / self.diffGoal

    def compute_reward_sparse(
        self,
        achieved_goal: np.ndarray,
        desired_goal: np.ndarray,
        info: dict,
    ) -> float:
        """
        Proximity to the goal is rewarded

        We use a weighted p-norm

        :param achieved_goal: the goal that was achieved
        :param desired_goal: the goal that was desired
        :param dict info: any supplementary information
        :param p: the Lp^p norm used in the reward. Use p<1 to
        have high kurtosis for rewards in [0, 1]
        :return: the corresponding reward
        """
        dst = np.array([np.linalg.norm(achieved_goal - desired_goal, axis=-1)]).T
        coeff_matrix = np.array(
            [
                [
                    -self.weight_command,
                    # -1,
                    # self.weight_alive,
                    # 0,
                    0,
                    0,
                    self.weight_target_reached,
                ],
            ],
        ).T
        info_matrix = np.empty((achieved_goal.shape[0], 3))

        for i, inf in enumerate(info):
            info_matrix[i] = inf["param_rew"]
        info_matrix = np.concatenate(
            (
                info_matrix,
                (dst < self.threshold_success).astype(int) - 0.002 * np.ones_like(dst),
            ),
            axis=1,
        )
        return info_matrix @ coeff_matrix

    def compute_reward_dense(
        self,
        achieved_goal: np.ndarray,
        desired_goal: np.ndarray,
        info: dict,
    ) -> float:
        dst = np.array([np.linalg.norm(achieved_goal - desired_goal, axis=-1)]).T
        coeff_matrix = np.array(
            [
                [
                    -self.weight_command,
                    # corresponds to the command penalization
                    -1,
                    # corresponds to the lenght to init penalization
                    self.weight_truncation,
                    # corresponds to the truncation penalization
                    -self.weight_target,
                    # corresponds to the distance to target
                ],
            ],
        ).T
        info_matrix = np.empty((achieved_goal.shape[0], 3))

        for i, inf in enumerate(info):
            info_matrix[i] = inf["param_rew"]
        info_matrix = np.concatenate(
            (info_matrix, dst),
            axis=1,
        )
        return info_matrix @ coeff_matrix
