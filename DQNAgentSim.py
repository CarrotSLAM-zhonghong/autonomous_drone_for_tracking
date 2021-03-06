from EnvironmentSim import EnvironmentSim

import os
import random
import numpy as np
import tensorflow as tf
from collections import deque
from keras.models import Sequential
from keras.layers import Convolution2D, Flatten, Dense, LSTM

class ReplayMemory(object):
    """
    ReplayMemory keeps track of the environment dynamic.
    We store all the transitions (s(t), action, s(t+1), reward, done).
    The replay memory allows us to efficiently sample minibatches from it, and generate the correct state representation
    (w.r.t the number of previous frames needed).
    """
    def __init__(self, size, sample_shape, history_length=4):
        self._pos            = 0
        self._count          = 0
        self._max_size       = size
        self._history_length = max(1, history_length)
        self._state_shape    = sample_shape
        self._states         = np.zeros((size,) + sample_shape, dtype=np.float32)
        self._actions        = np.zeros(size, dtype=np.uint8)
        self._rewards        = np.zeros(size, dtype=np.float32)
        self._terminals      = np.zeros(size, dtype=np.float32)

    def __len__(self):
        """ Returns the number of items currently present in the memory
        """
        return self._count

    def append(self, state, action, reward, done):
        """ Appends the specified transition to the memory.
        """
        assert state.shape == self._state_shape, \
            'Invalid state shape (required: %s, got: %s)' % (self._state_shape, state.shape)

        self._states[self._pos]    = state
        self._actions[self._pos]   = action
        self._rewards[self._pos]   = reward
        self._terminals[self._pos] = done

        self._count = max(self._count, self._pos + 1)
        self._pos   = (self._pos + 1) % self._max_size

    def sample(self, size):
        """ Generate size random integers mapping indices in the memory.
            The returned indices can be retrieved using #get_state().
            See the method #minibatch() if you want to retrieve samples directly.
        """
        # Local variable access is faster in loops
        count, pos, history_len, terminals = self._count - 1, self._pos, \
                                             self._history_length, self._terminals
        indexes = []

        while len(indexes) < size:
            index = np.random.randint(history_len, count)

            if index not in indexes:

                # if not wrapping over current pointer,
                # then check if there is terminal state wrapped inside
                if not (index >= pos > index - history_len):
                    if not terminals[(index - history_len):index].any():
                        indexes.append(index)

        return indexes

    def minibatch(self, size):
        """ Generate a minibatch with the number of samples specified by the size parameter.
        """
        indexes = self.sample(size)

        pre_states  = np.array([self.get_state(index) for index in indexes], dtype=np.float32)
        post_states = np.array([self.get_state(index + 1) for index in indexes], dtype=np.float32)
        actions     = self._actions[indexes]
        rewards     = self._rewards[indexes]
        dones       = self._terminals[indexes]

        return pre_states, actions, post_states, rewards, dones

    def get_state(self, index):
        """
        Return the specified state with the replay memory. A state consists of
        the last `history_length` perceptions.
        """
        if self._count == 0:
            raise IndexError('Empty Memory')

        index         %= self._count
        history_length = self._history_length

        # If index > history_length, take from a slice
        if index >= history_length:
            return self._states[(index - (history_length - 1)):index + 1, ...]
        else:
            indexes = np.arange(index - history_length + 1, index + 1)
            return self._states.take(indexes, mode='wrap', axis=0)


class History(object):
    """
    Accumulator keeping track of the N previous frames to be used by the agent
    for evaluation
    """
    def __init__(self, shape):
        self._buffer = np.zeros(shape, dtype=np.float32)

    @property
    def value(self):
        """ Underlying buffer with N previous states stacked along first axis
        """
        return self._buffer

    def append(self, state):
        """ Append state to the history
        """
        self._buffer[:-1] = self._buffer[1:]
        self._buffer[-1]  = state

    def reset(self):
        """ Reset the memory. Underlying buffer set all indexes to 0
        """
        self._buffer.fill(0)


class DeepQAgent(object):
    """
    Implementation of Deep Q Neural Network agent like in:
    Nature 518. "Human-level control through deep reinforcement learning" (Mnih & al. 2015)
    """

    STATE_LENGTH           = 4  # Number of most recent frames to produce the input to the network
    GAMMA                  = 0.99  # Discount factor
    EXPLORATION_STEPS      = 10000  # Number of steps over which the initial value of epsilon is linearly annealed to its final value
    INITIAL_EPSILON        = 1.0  # Initial value of epsilon in epsilon-greedy
    FINAL_EPSILON          = 0.1  # Final value of epsilon in epsilon-greedy
    INITIAL_REPLAY_SIZE    = 10000  # Number of steps to populate the replay memory before training starts
    MEMORY_SIZE            = 400000  # Number of replay memory the agent uses for training
    BATCH_SIZE             = 64  # Mini batch size
    TARGET_UPDATE_INTERVAL = 10000  # The frequency with which the target network is updated
    TRAIN_AFTER            = 500 # Number of Steps after which training starts
    TRAIN_INTERVAL         = 4  # The agent selects 4 actions between successive updates
    LEARNING_RATE          = 0.00025  # Learning rate used by RMSProp
    MOMENTUM               = 0.95  # Momentum used by RMSProp
    MIN_GRAD               = 0.01  # Constant added to the squared gradient in the denominator of the RMSProp update
    SAVE_INTERVAL          = 10000  # The frequency with which the network is saved
    LOAD_NETWORK           = False
    SAVE_NETWORK_PATH      = 'models_sim'
    SAVE_SUMMARY_PATH      = 'logs'

    def __init__(self, input_shape, nb_actions):
        self.t            = 0
        self.epsilon      = self.INITIAL_EPSILON
        self.epsilon_step = (self.INITIAL_EPSILON - self.FINAL_EPSILON) / self.EXPLORATION_STEPS

        self.total_reward  = 0.0
        self.total_q_max   = 0.0
        self.total_loss    = 0
        self.duration      = 0
        self.episode       = 0

        self.input_shape  = input_shape
        self.nb_actions   = nb_actions

        self._history           = History(input_shape)
        self._memory            = ReplayMemory(self.MEMORY_SIZE, input_shape[1:], self.STATE_LENGTH)
        self._num_actions_taken = 0

        # Action Value model (used by agent to interact with the environment)
        self.s, self.q_values, q_network = self.build_network(self.input_shape)
        q_network_weights = q_network.trainable_weights

        # Target model used to compute the target Q-values in training, updated
        # less frequently for increased stability.
        self.st, self.target_q_values, target_network = self.build_network(self.input_shape)
        target_network_weights = target_network.trainable_weights

        # Define target network update operation
        self.update_target_network = [target_network_weights[i].assign(q_network_weights[i]) for i in range(len(target_network_weights))]

        # Define loss and gradient update operation
        self.a, self.y, self.loss, self.grads_update = self.build_training_op(q_network_weights)

        self.sess  = tf.InteractiveSession()
        self.saver = tf.train.Saver(q_network_weights)

        self.summary_placeholders, self.update_ops, self.summary_op = self.setup_summary()
        self.summary_writer = tf.summary.FileWriter(self.SAVE_SUMMARY_PATH, self.sess.graph)

        if not os.path.exists(self.SAVE_NETWORK_PATH):
            os.makedirs(self.SAVE_NETWORK_PATH)

        self.sess.run(tf.initialize_all_variables())

        # Load network
        if self.LOAD_NETWORK:
            self.load_network()

        # Initialize target network
        self.sess.run(self.update_target_network)

    def build_network(self, input_shape):
        model = Sequential()
        model.add(LSTM(16, input_shape=input_shape)) # 32
        model.add(Dense(32, activation='relu')) # 64
        model.add(Dense(32, activation='relu')) # 32
        model.add(Dense(self.nb_actions))

        s = tf.placeholder(tf.float32, (None,) + input_shape)
        q_values = model(s)

        return s, q_values, model

    def build_training_op(self, q_network_weights):
        a = tf.placeholder(tf.int64, [None])
        y = tf.placeholder(tf.float32, [None])

        # Convert action to one hot vector
        a_one_hot = tf.one_hot(a, self.nb_actions, 1.0, 0.0)
        q_value   = tf.reduce_sum(tf.multiply(self.q_values, a_one_hot), reduction_indices=1)

        # Clip the error, the loss is quadratic when the error is in (-1, 1), and linear outside of that region
        error          = tf.abs(y - q_value)
        quadratic_part = tf.clip_by_value(error, 0.0, 1.0)
        linear_part    = error - quadratic_part
        loss           = tf.reduce_mean(0.5 * tf.square(quadratic_part) + linear_part)

        optimizer    = tf.train.RMSPropOptimizer(self.LEARNING_RATE, momentum=self.MOMENTUM, epsilon=self.MIN_GRAD)
        grads_update = optimizer.minimize(loss, var_list=q_network_weights)

        return a, y, loss, grads_update

    def act(self, state):
        """ This allows the agent to select the next action to perform in regard of the current state of the environment.
        It follows the terminology used in the Nature paper.
        """
        # Append the state to the short term memory (ie. History)
        self._history.append(state)

        if self.epsilon >= random.random() or self.t < self.INITIAL_REPLAY_SIZE:
            # Choose an action randomly
            action = random.randrange(self.nb_actions)
        else:
            # Use the network to output the best action
            env_with_history = self._history.value
            action = np.argmax(self.q_values.eval(feed_dict={self.s: env_with_history.reshape((1,) + env_with_history.shape)}))

        # Anneal epsilon linearly over time
        if self.epsilon > self.FINAL_EPSILON and self.t >= self.INITIAL_REPLAY_SIZE:
            self.epsilon -= self.epsilon_step

        # Keep track of interval action counter
        self._num_actions_taken += 1
        return action

    def observe(self, old_state, action, reward, done):
        """ This allows the agent to observe the output of doing the action it selected through act() on the old_state
        """
        self.total_reward += reward

        # If done, reset short term memory (ie. History)
        self.total_reward += reward
        env_with_history = self._history.value
        self.total_q_max += np.max(self.q_values.eval(feed_dict={self.s: env_with_history.reshape((1,) + env_with_history.shape)}))
        self.duration += 1

        if done:
            # Write summary
            if self.t >= self.INITIAL_REPLAY_SIZE:
                stats = [self.total_reward, self.total_q_max / float(self.duration),
                        self.duration, self.total_loss / (float(self.duration) / float(self.TRAIN_INTERVAL))]
                for i in range(len(stats)):
                    self.sess.run(self.update_ops[i], feed_dict={
                        self.summary_placeholders[i]: float(stats[i])
                    })
                summary_str = self.sess.run(self.summary_op)
                self.summary_writer.add_summary(summary_str, self.episode + 1)

            # Debug
            if self.t < self.INITIAL_REPLAY_SIZE:
                mode = 'random'
            elif self.INITIAL_REPLAY_SIZE <= self.t < self.INITIAL_REPLAY_SIZE + self.EXPLORATION_STEPS:
                mode = 'explore'
            else:
                mode = 'exploit'
            print "-----EPISODE SUMMARY-----"
            print "EPISODE    :", self.episode + 1, \
                "\nTIMESTEP   :", self.t, \
                "\nDURATION   :", self.duration, \
                "\nEPSILON    :", self.epsilon, \
                "\nTOTALREWARD:", self.total_reward, \
                "\nAVG_MAX_Q  :", self.total_q_max / float(self.duration), \
                "\nAVG_LOSS   :", self.total_loss / float(self.duration), \
                "\nMODE       :", mode

            print "-------------------------"

            self.total_reward = 0
            self.total_q_max = 0
            self.total_loss = 0
            self.duration = 0
            self.episode += 1

            # Reset the short term memory
            self._history.reset()

        # Append to long term memory
        self._memory.append(old_state, action, reward, done)

    def train(self):
        """ This allows the agent to train itself to better understand the environment dynamics.
        The agent will compute the expected reward for the state(t+1)
        and update the expected reward at step t according to this.

        The target expectation is computed through the Target Network, which is a more stable version
        of the Action Value Network for increasing training stability.

        The Target Network is a frozen copy of the Action Value Network updated as regular intervals.
        """
        agent_step = self._num_actions_taken
        if agent_step >= self.TRAIN_AFTER:
            if (agent_step % self.TRAIN_INTERVAL) == 0:
                # Clip all positive rewards at 1 and all negative rewards at -1, leaving 0 rewards unchanged
                # reward = np.clip(reward, -1, 1)
                print "Episode    :", self.episode, \
                    "\nTimestep   :", self.t, \
                    "\nAgent Step :", agent_step

                if self.t >= self.INITIAL_REPLAY_SIZE:
                    # Train network
                    if self.t % self.TRAIN_INTERVAL == 0:
                        self.train_network()

                    # Update target network
                    if self.t % self.TARGET_UPDATE_INTERVAL == 0:
                        self.sess.run(self.update_target_network)

                    # Save network
                    if self.t % self.SAVE_INTERVAL == 0:
                        save_path = self.saver.save(self.sess, self.SAVE_NETWORK_PATH + '/chkpnt', global_step=self.t)
                        print "Successfully saved:", save_path

                self.t += 1

    def train_network(self):
        ''' Extension to train() call - Batch generation and graph computations
        '''
        # Sample random minibatch of transition from replay memory
        state_batch, action_batch, next_state_batch, reward_batch, terminal_batch = self._memory.minibatch(self.BATCH_SIZE)

        target_q_values_batch = self.target_q_values.eval(feed_dict={self.st: next_state_batch})
        y_batch               = reward_batch + (1 - terminal_batch) * self.GAMMA * np.max(target_q_values_batch, axis=1)

        loss, _ = self.sess.run([self.loss, self.grads_update], feed_dict={
            self.s: state_batch,
            self.a: action_batch,
            self.y: y_batch
        })
        self.total_loss += loss

    def setup_summary(self):
        episode_total_reward = tf.Variable(0.)
        episode_avg_max_q    = tf.Variable(0.)
        episode_duration     = tf.Variable(0.)
        episode_avg_loss     = tf.Variable(0.)

        tf.summary.scalar('logs/Total Reward/Episode', episode_total_reward)
        tf.summary.scalar('logs/Average Max Q/Episode', episode_avg_max_q)
        tf.summary.scalar('logs/Duration/Episode', episode_duration)
        tf.summary.scalar('logs/Average Loss/Episode', episode_avg_loss)

        summary_vars         = [episode_total_reward, episode_avg_max_q, episode_duration, episode_avg_loss]
        summary_placeholders = [tf.placeholder(tf.float32) for _ in range(len(summary_vars))]
        update_ops           = [summary_vars[i].assign(summary_placeholders[i]) for i in range(len(summary_vars))]
        summary_op           = tf.summary.merge_all()

        return summary_placeholders, update_ops, summary_op

    def load_network(self):
        checkpoint = tf.train.get_checkpoint_state(self.SAVE_NETWORK_PATH)
        if checkpoint and checkpoint.model_checkpoint_path:
            self.saver.restore(self.sess, checkpoint.model_checkpoint_path)
            print('Successfully loaded: ' + checkpoint.model_checkpoint_path)
        else:
            print('Training new network...')

    def test(self, state):
        self.t += 1
        self._history.append(state)

        if self.t >= self.STATE_LENGTH:
            env_with_history = self._history.value
            action = np.argmax(self.q_values.eval(feed_dict={self.s: env_with_history.reshape((1,) + env_with_history.shape)}))
            return action
        return None


def interpret_action(action):
    scaling_factor = 0.25
    if action == 0:
        quad_offset = (0, 0, 0)
    elif action == 1:
        quad_offset = (scaling_factor, 0, 0)
    elif action == 2:
        quad_offset = (0, scaling_factor, 0)
    elif action == 3:
        quad_offset = (-scaling_factor, 0, 0)
    elif action == 4:
        quad_offset = (0, -scaling_factor, 0)

    return quad_offset

# def interpret_action_seq(action, step_sizes):
#     action_size = len(step_sizes)
#     quad_offset = (step_sizes[action%action_size], step_sizes[action/action_size])
#     name        = str(quad_offset) + " m"
#
#     return quad_offset, name


def restart_game():
    return env.reset()


if __name__=='__main__':
    TEST             = False # False
    # Make RL agent
    input_dims       = 2 # 8
    num_actions      = 5 # 7
    num_buff_frames  = 4
    im_width         = 1280
    im_height        = 720
    max_guided_eps   = 1000


    agent = DeepQAgent((num_buff_frames, input_dims), num_actions)

    if not TEST:
        # Train
        env           = EnvironmentSim(image_shape=(im_height, im_width), max_guided_eps=max_guided_eps)
        current_state = env.reset()

        while True:
            action            = agent.act(current_state)
            # quad_offset, name = interpret_action_seq(action, step_sizes)
            quad_offset       = interpret_action(action)
            new_state, reward, done = env.step(quad_offset)
            agent.observe(current_state, action, reward, done)
            agent.train()

            if done:
                print "Restarting the Game"
                new_state = restart_game()

            current_state = new_state
            print "--------------------\n"
    else:
        # Test
        env           = EnvironmentSim(image_shape=(im_height, im_width))
        current_state = env.reset()

        while True:
            action = agent.test(current_state)
            quad_offset = None
            if action is not None:
                quad_offset, _ = interpret_action_seq(action, step_sizes)
            new_state, _, _ = env.step(quad_offset)
            current_state = new_state
