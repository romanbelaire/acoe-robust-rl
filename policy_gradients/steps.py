import functools
import torch as ch
import torch.nn.functional as F
from torch.nn.utils.rnn import pad_sequence, pack_padded_sequence, pad_packed_sequence
import numpy as np
import math
from tqdm import tqdm
from torch.nn.utils import parameters_to_vector as flatten
from torch.nn.utils import vector_to_parameters as assign
from .torch_utils import *
from .convex_relaxation import get_kl_bound as get_state_kl_bound
from .convex_relaxation import *
from policy_gradients.pgd_act import worst_action_pgd, worst_state_pgd
from policy_gradients.models import CtsLSTMPolicy, ValueLSTMNet

'''
File for taking steps in both policy and value network space.
Layout of this file:
    - Surrogate reward function
    - Logging functions for TRPO approximations
        - kl_approximation_logging
        - kl_vs_second_order_approx
    - Possible value loss functions
        - consistency loss [+ clipped version for matching OpenAI]
        - time-dependent baseline
    - Actual optimization functions
        - value_step
        - ppo_step
        - trpo_step
'''
def get_mean_bound(model, x, means, eps, beta=None, stdev=None, use_full_backward=False):
        # Set each layer's perturbation eps and log_stdev's perturbation.
        x = BoundedTensor(x, ptb=PerturbationLpNorm(norm=np.inf, eps=eps)).requires_grad_(False)
        if forward_one:
            inputs = (x, )
        else:
            inputs = (x, means)
        ilb, iub = model.compute_bounds(inputs, IBP=True, C=None, method=None, bound_lower=True, bound_upper=True)
        return iub, ilb

def adv_normalize(adv, mask=None):
    if mask is None:
        if adv.nelement() == 1:
            return adv
        std = adv.std()
        mean = adv.mean()
    else:
        masked_adv = adv[mask]
        if masked_adv.nelement() == 1:
            return adv
        std = masked_adv.std()
        mean = masked_adv.mean()

    if (ch.isnan(std)):
        print(adv, std, mean, mask)
    
    assert not ch.isnan(std), 'Need nonzero std'
    n_advs = (adv - mean)/(std + 1e-8)
    return n_advs

def surrogate_reward(adv, *, new, old, clip_eps=None, mask=None, normalize=True):
    '''
    Computes the surrogate reward for TRPO and PPO:
    R(\theta) = E[r_t * A_t]
    with support for clamping the ratio (for PPO), s.t.
    R(\theta) = E[clamp(r_t, 1-e, 1+e) * A_t]
    Inputs:
    - adv, unnormalized advantages as calculated by the agents
    - log_ps_new, the log probabilities assigned to taken events by \theta_{new}
    - log_ps_old, the log probabilities assigned to taken events by \theta_{old}
    - clip_EPS, the clipping boundary for PPO loss
    Returns:
    - The surrogate loss as described above
    '''
    log_ps_new, log_ps_old = new, old

    if normalize:
        # Normalized Advantages
        n_advs = adv_normalize(adv, mask)
    else:
        n_advs = adv

    assert shape_equal_cmp(log_ps_new, log_ps_old, n_advs)

    # Ratio of new probabilities to old ones
    ratio_new_old = ch.exp(log_ps_new - log_ps_old)

    # Clamping (for use with PPO)
    if clip_eps is not None:
        ratio_new_old = ch.clamp(ratio_new_old, 1-clip_eps, 1+clip_eps)
    return ratio_new_old * n_advs

######
# Possible Loss Functions for the value network
# Supports consistency loss, time-dependent baseline, OpenAI loss
# Also logs explained variance = MSE(values, targets)/Var[targets]
#####
def surrogate_adv_q(adv, *, new, old, ccer, q_weight=0, clip_eps=None, mask=None, normalize=True, regret_q=True, use_wocar=None, acoe_advs=False):
    '''
    Computes the surrogate reward for TRPO and PPO:
    R(\theta) = E[r_t * A_t]
    with support for clamping the ratio (for PPO), s.t.
    R(\theta) = E[clamp(r_t, 1-e, 1+e) * A_t]
    Inputs:
    - adv, unnormalized advantages as calculated by the agents
    - log_ps_new, the log probabilities assigned to taken events by \theta_{new}
    - log_ps_old, the log probabilities assigned to taken events by \theta_{old}
    - clip_EPS, the clipping boundary for PPO loss
    - regret_q: the "ccer" parameter contains regret, and not worst_q
    - use_wocar: incorporate wocar min_q method as well as ccer_q. This parameter exists because CCER-WPPO uses both wocar and ccer
    Returns:
    - The surrogate loss as described above
    '''
    log_ps_new, log_ps_old = new, old
    assert regret_q or use_wocar or acoe_advs
    
    if normalize:
        # Normalized Advantages
        n_advs = adv_normalize(adv, mask)
        n_ccer = adv_normalize(ccer, mask)
        if use_wocar:
            n_worst_q = adv_normalize(use_wocar, mask)
    else:
        n_advs = adv
        n_ccer = ccer
        n_worst_q = use_wocar

    assert shape_equal_cmp(log_ps_new, log_ps_old, n_advs)

    # Ratio of new probabilities to old ones
    ratio_new_old = ch.exp(log_ps_new - log_ps_old)

    # Clamping (for use with PPO)
    if clip_eps is not None:
        ratio_new_old = ch.clamp(ratio_new_old, 1-clip_eps, 1+clip_eps)

    if regret_q:
        if use_wocar:
            robust_adv = n_advs - (q_weight * n_ccer) + (q_weight * n_worst_q)#subtract regret (ccer), when regret is low the advantage is high
        else:
            if acoe_advs:
                robust_adv = n_advs
            else:
                robust_adv = n_advs - (q_weight * n_ccer) #subtract regret (ccer), when regret is low the advantage is high
    else:#min_q only, for some reason
        robust_adv = n_advs + (q_weight * n_ccer) #add min reward (ccer parameter here should instead hold the min_q)
    return ratio_new_old * robust_adv

def value_loss_gae(vs, _, advantages, not_dones, params, old_vs, mask=None, store=None, re=False, reduction='mean'):
    '''
    GAE-based loss for the value function:
        L_t = ((v_t + A_t).detach() - v_{t})
    Optionally, we clip the value function around the original value of v_t

    Inputs: rewards, returns, not_dones, params (from value_step)
    Outputs: value function loss
    '''
    # Desired values are old values plus advantage of the action taken. They do not change during the optimization process.
    # We want the current values are close to them.
    val_targ = (old_vs + advantages).detach()
    assert shape_equal_cmp(val_targ, vs, not_dones, old_vs, advantages)
    assert len(vs.shape) == 1 or len(vs.shape) == 2

    try:
        vs_clipped = old_vs + ch.clamp(vs - old_vs, -params.CLIP_VAL_EPS, params.CLIP_VAL_EPS)
    except AttributeError as e:
        vs_clipped = old_vs + ch.clamp(vs - old_vs, -params.CLIP_EPS, params.CLIP_EPS)
        
    # Don't incur loss from last timesteps (since there's no return to use)
    sel = ch.logical_and(not_dones.bool(), mask)
    # print('selected', sel.sum().item())
    assert shape_equal_cmp(vs, sel)
    val_loss_mat_unclipped = (vs - val_targ)[sel].pow(2)
    val_loss_mat_clipped = (vs_clipped - val_targ)[sel].pow(2)

    # In OpenAI's PPO implementation, we clip the value function around the previous value estimate
    # and use the worse of the clipped and unclipped versions to train the value function

    # Presumably the inspiration for this is similar to PPO
    if params.VALUE_CLIPPING:
        val_loss_mat = ch.max(val_loss_mat_unclipped, val_loss_mat_clipped)
    else:
        val_loss_mat = val_loss_mat_unclipped

    # assert shape_equal_cmp(val_loss_mat, vs)
    # Mean squared loss
    if reduction == 'mean':
        mse = val_loss_mat.mean()
    elif reduction == 'sum':
        mse = val_loss_mat.sum()
    else:
        raise ValueError('Unknown reduction ' + reduction)

    if re:
        # Relative error.
        se = not_dones.bool()
        relerr = val_loss_mat/val_targ[se].abs()
        mre = relerr.abs().mean()
        msre = relerr.pow(2).mean()
        return mse, mre, msre

    return mse

def value_loss_returns(vs, returns, advantages, not_dones, params, old_vs,
                       mask=None, store=None, re=False):
    '''
    Returns (with time input) loss for the value function:
        L_t = (R_t - v(s, t))
    Inputs: rewards, returns, not_dones, params (from value_step)
    Outputs: value function loss
    '''
    assert shape_equal_cmp(vs, returns)
    sel = not_dones.bool()
    val_loss_mat = (vs - returns)[sel]
    mse = val_loss_mat.pow(2).mean()
    val_targ = returns

    if re:
        relerr = val_loss_mat/val_targ[sel].abs()
        mre = relerr.abs().mean()
        msre = relerr.pow(2).mean()
        return mse, mre, msre

    return mse

###
# Optimization functions for the value and policy parameters
# value_step, ppo_step, trpo_step
###
def value_step(all_states, returns, advantages, not_dones, net,
               val_opt, params, store, old_vs=None, opt_step=None,
               should_tqdm=False, should_cuda=False, test_saps=None):
    '''
    Take an optimizer step fitting the value function
    parameterized by a neural network
    Inputs:
    - all_states, the states at each timestep
    - rewards, the rewards gained at each timestep
    - returns, discounted rewards (ret_t = r_t + gamma*ret_{t+1})
    - advantaages, estimated by GAE
    - not_dones, N * T array with 0s at final steps and 1s everywhere else
    - net, the neural network representing the value function 
    - val_opt, the optimizer for net
    - params, dictionary of parameters
    Returns:
    - Loss of the value regression problem
    '''

    # (sharing weights) XOR (old_vs is None)
    # assert params.SHARE_WEIGHTS ^ (old_vs is None)

    # Options for value function
    VALUE_FUNCS = {
        "gae": value_loss_gae,
        "time": value_loss_returns
    }
    # If we are not sharing weights, then we need to keep track of what the 
    # last value was here. If we are sharing weights, this is handled in policy_step
    with ch.no_grad():
        if old_vs is None:
            state_indices = np.arange(returns.nelement())
            # No shuffling, just split an sequential list of indices.
            splits = np.array_split(state_indices, params.NUM_MINIBATCHES)
            orig_vs = []
            # Minibatch.
            for selected in splits:
                # Values of current network prediction.
                orig_vs.append(net(all_states[selected]).squeeze(-1))
            orig_vs = ch.cat(orig_vs)
            old_vs = orig_vs.detach()
        if test_saps is not None:
            old_test_vs = net(test_saps.states).squeeze(-1)


    """
    print('all_states', all_states.size())
    print('returns', returns.size())
    print('advantages', advantages.size())
    print('not_dones', not_dones.size())
    print('old_vs', old_vs.size())
    """


    r = range(params.VAL_EPOCHS) if not should_tqdm else \
                            tqdm(range(params.VAL_EPOCHS))

    if params.HISTORY_LENGTH > 0 and params.USE_LSTM_VAL:
        # LSTM policy. Need to go over all episodes instead of states.
        batches, alive_masks, time_masks, lengths = pack_history([all_states, returns, not_dones, advantages, old_vs], not_dones, max_length=params.HISTORY_LENGTH)
        print("Value lengths: ", lengths)
        assert not params.SHARE_WEIGHTS

    for i in r:
        if params.HISTORY_LENGTH > 0 and params.USE_LSTM_VAL:
            # LSTM policy. Need to go over all episodes instead of states.
            hidden = None
            net.lstm.flatten_parameters()
            val_opt.zero_grad()
            val_loss = 0.0
            for i, batch in enumerate(batches):
                # Now we get chunks of time sequences, each of them with a maximum length of params.HISTORY_LENGTH.
                # select log probabilities, advantages of this minibatch.
                batch_states, batch_returns, batch_not_dones, batch_advs, batch_old_vs = batch
                mask = time_masks[i]
                # keep only the alive hidden states.
                if hidden is not None:
                    # print('hidden[0]', hidden[0].size())
                    hidden = [h[:, alive_masks[i], :].detach() for h in hidden]
                    # print('hidden[0]', hidden[0].size())
                vs, hidden = net.multi_forward(batch_states, hidden=hidden)
                vs = vs.squeeze(-1)
                """
                print('vs', vs.size())
                print('batch_states', batch_states.size())
                print('batch_returns', batch_returns.size())
                print('batch_not_dones', batch_not_dones.size())
                print('batch_advs', batch_advs.size())
                print('batch_old_vs', batch_old_vs.size())
                input()
                """
                """
                print('old')
                print(batch_old_vs)
                print('new')
                print(vs * mask)
                print('diff')
                print((batch_old_vs - vs * mask).pow(2).sum().item())
                input()
                """
                vf = VALUE_FUNCS[params.VALUE_CALC]
                batch_val_loss = vf(vs, batch_returns, batch_advs, batch_not_dones, params,
                              batch_old_vs, mask=mask, store=store, reduction='sum')
                val_loss += batch_val_loss

            val_loss = val_loss / all_states.size(0)
            val_loss.backward()
            val_opt.step()
        else:
            # Create minibatches with shuffuling
            state_indices = np.arange(returns.nelement())
            np.random.shuffle(state_indices)
            splits = np.array_split(state_indices, params.NUM_MINIBATCHES)

            assert shape_equal_cmp(returns, advantages, not_dones, old_vs)

            # Minibatch SGD
            for selected in splits:
                val_opt.zero_grad()

                def sel(*args):
                    return [v[selected] for v in args]

                def to_cuda(*args):
                    return [v.to("cuda:".format(params.CUDA_ID)) for v in args]

                # Get a minibatch (64) of returns, advantages, etc.
                tup = sel(returns, advantages, not_dones, old_vs, all_states)
                mask = ch.tensor(True)

                if should_cuda: tup = to_cuda(*tup)
                sel_rets, sel_advs, sel_not_dones, sel_ovs, sel_states = tup

                # Value prediction of current network given the states.
                vs = net(sel_states).squeeze(-1)

                vf = VALUE_FUNCS[params.VALUE_CALC]
                val_loss = vf(vs, sel_rets, sel_advs, sel_not_dones, params,
                              sel_ovs, mask=mask, store=store)

                # If we are sharing weights, then value_step gets called 
                # once per policy optimizer step anyways, so we only do one batch
                if params.SHARE_WEIGHTS:
                    return val_loss

                # From now on, params.SHARE_WEIGHTS must be False
                val_loss.backward()
                val_opt.step()
        if should_tqdm:
            if test_saps is not None: 
                vs = net(test_saps.states).squeeze(-1)
                test_loss = vf(vs, test_saps.returns, test_saps.advantages,
                    test_saps.not_dones, params, old_test_vs, None)
            r.set_description(f'vf_train: {val_loss.mean().item():.2f}'
                              f'vf_test: {test_loss.mean().item():.2f}')
        print(f'val_loss={val_loss.item():8.5f}')

    return val_loss

def soft_update(target, source, tau):
    for target_param, param in zip(target.parameters(), source.parameters()):
        target_param.data.copy_(target_param.data * (1.0 - tau) + param.data * tau)
        
def worst_q_step(all_states, actions, next_states, not_dones, rewards, q_net, target_q_net, policy_net, q_opt,     
                params, eps_scheduler, should_tqdm=False, should_cuda=False):
    '''
    Take an optimizer step training the worst-q function
    parameterized by a neural network
    Inputs:
    - all_states, the states at each timestep
    - actions, the actions taking at each timestep
    - next_states, the next states after taking actions
    - not dones, N * T array with 0s at final steps and 1s everywhere else
    - rewards, the rewards gained at each timestep
    - q_net, worst-case q neural network
    - q_opt, the optimizer for q_net
    - target_q_net, the target q_net
    - params, dictionary of parameters
    Returns:
    - Loss of the q_net regression problem
    '''
    current_eps = eps_scheduler.get_eps()
    discrete = policy_net.discrete

    r = range(params.Q_EPOCHS) if not should_tqdm else \
                            tqdm(range(params.Q_EPOCHS))
    for i in r:
        # Create minibatches with shuffuling
        state_indices = np.arange(rewards.nelement())
        np.random.shuffle(state_indices)
        splits = np.array_split(state_indices, params.NUM_MINIBATCHES)

        assert shape_equal_cmp(rewards, not_dones)

        # Minibatch SGD
        for selected in splits:
            q_opt.zero_grad()

            def sel(*args):
                return [v[selected] for v in args]

            def to_cuda(*args):
                return [v.cuda() for v in args]

            # Get a minibatch (64).
            tup = sel(actions, rewards, not_dones, next_states, all_states)
            if should_cuda: tup = to_cuda(*tup)
            sel_acts, sel_rews, sel_not_dones, sel_next_states, sel_states = tup
            # Worst q prediction of current network given the states.
            concat_input = sel_states if discrete else ch.cat((sel_states, sel_acts), dim=1)
            curr_q = q_net(concat_input).squeeze(-1)
            if not discrete:
                worst_actions = worst_action_pgd(q_net, policy_net, sel_next_states, eps=0.01, maxiter=50, not_dones=sel_not_dones)
            concat_worst_next_input = sel_next_states if discrete else ch.cat((sel_next_states, worst_actions), dim=1)
            expected_q = sel_rews + params.GAMMA * sel_not_dones * target_q_net(concat_worst_next_input).squeeze(-1)
            '''
            print('curr_q', curr_q.mean())
            print('expected_q', expected_q.mean())
            '''
            q_loss = F.mse_loss(curr_q, expected_q)
            q_loss.backward()
            q_opt.step()
            soft_update(target_q_net, q_net, params.TAU)

        print(f'q_loss={q_loss.item():8.5f}')

    return q_loss

def ccer_q_step(all_states, actions, next_states, not_dones, rewards, ccer_q_net, target_q_net, policy_net, min_q_net, ccer_opt,     
                params, eps_scheduler, should_tqdm=False, should_cuda=False):
    '''
    Take an optimizer step training the CCER function
    parameterized by a neural network
    Inputs:
    - all_states, the states at each timestep
    - actions, the actions taking at each timestep
    - next_states, the next states after taking actions
    - not dones, N * T array with 0s at final steps and 1s everywhere else
    - rewards, the rewards gained at each timestep
    - ccer_q_net, ccer neural network
    - ccer_opt, the optimizer for ccer_q_net
    - target_q_net, the target ccer_q_net
    - min_q_net, evaluates the minimum Q value, so we can pull estimated rewards from it
    - params, dictionary of parameters
    Returns:
    - Loss of the ccer_q_net regression problem
    '''
    current_eps = eps_scheduler.get_eps()

    r = range(params.Q_EPOCHS) if not should_tqdm else \
                            tqdm(range(params.Q_EPOCHS))
    discrete = policy_net.discrete
    for i in r:
        # Create minibatches with shuffuling
        state_indices = np.arange(rewards.nelement())
        np.random.shuffle(state_indices)
        splits = np.array_split(state_indices, params.NUM_MINIBATCHES)

        assert shape_equal_cmp(rewards, not_dones)

        # Minibatch SGD
        for selected in splits:
            ccer_opt.zero_grad()

            def sel(*args):
                return [v[selected] for v in args]

            def to_cuda(*args):
                return [v.cuda() for v in args]

            # Get a minibatch (64).
            tup = sel(actions, rewards, not_dones, next_states, all_states)

            if should_cuda: tup = to_cuda(*tup)
            sel_acts, sel_rews, sel_not_dones, sel_next_states, sel_states = tup

            # Worst ccer prediction of current network given the states.
            concat_input = sel_states if discrete else ch.cat((sel_states, sel_acts), dim=1)
            ccer = ccer_q_net(concat_input).squeeze(-1)
            worst_actions = worst_action_pgd(ccer_q_net, policy_net, sel_states, eps=0.01, maxiter=50, not_dones=sel_not_dones)
            worst_next_actions = worst_action_pgd(ccer_q_net, policy_net, sel_next_states, eps=0.01, maxiter=50, not_dones=sel_not_dones)
            next_actions = policy_net(ch.Tensor(sel_next_states))[0]
            
            #proper min_reward calc:
            concat_worst_input = sel_states if discrete else ch.cat((sel_states, worst_acts), dim=1) 
            concat_worst_next_input = sel_next_states if discrete else ch.cat((sel_next_states, worst_next_actions), dim=1)
            concat_next_input = sel_next_states if discrete else ch.cat((sel_next_states, next_acts), dim=1)

            min_rews = min_q_net(concat_worst_input).squeeze(-1) - min_q_net(concat_worst_next_input).squeeze(-1) # q-q_next = r
            expected_ccer = sel_rews - min_rews + params.GAMMA * sel_not_dones * target_q_net(concat_next_input).squeeze(-1)
            '''
            print('curr_q', curr_q.mean())
            print('expected_q', expected_q.mean())
            '''
            q_loss = F.mse_loss(ccer, expected_ccer)
            q_loss.backward()
            ccer_opt.step()
            soft_update(target_q_net, ccer_q_net, params.TAU)

        print(f'ccer_loss={q_loss.item():8.5f}')

    return q_loss

def soarl_q_step(all_states, actions, next_states, not_dones, rewards, ccer_q_net, target_q_net, policy_net, min_q_net, ccer_opt,     
                params, eps_scheduler, posterior_data, num_actions, should_tqdm=False, should_cuda=False):
    '''
    deprecated
    '''
    return None

def Lp_ball(state, epsilon=0.1, sample_size=10):#computes infinity-norm ball around state
    dist = np.random.default_rng().uniform(low=-1, high=1, size=(sample_size, state.numel()))
    softmax_dist = np.exp(dist)/np.expand_dims(np.sum(np.exp(dist), axis=1), axis=1).repeat(dist.shape[-1], axis=1)#dist / np.linalg.norm(dist, axis=0)
    scaled_normalized_dist = softmax_dist * epsilon
    scaled_normalized_dist = torch.Tensor(scaled_normalized_dist).view(-1, *state.shape[-2:]).to(state.device)
    return torch.Tensor((scaled_normalized_dist*state) + state).to(state.device)

def KL_dist(state1, state2, net, is_action=False):
    '''
    Computes D_KL(π(state1)||π(state2))
    state1: true state, expectation is computed w.r.t. this state
    state2: predicted/observed state
    net: the policy net
    note: torch kl_div uses the "true" state as the second argument.
    '''
    if is_action: #input is already an action distribution
        return net.calc_kl(state1, state2)
    if state1.shape[0] == 1:
        dkl = net.calc_kl(net(state1.unsqueeze(0)),net(state2.unsqueeze(0)))
        return dkl[0]
    dkl = net.calc_kl(net(state1),net(state2))
    return dkl

def acoe_step(all_states, actions, next_states, not_dones, rewards, q_net, target_q_net, policy_net, min_q_net, q_opt,     
                params, eps_scheduler, num_actions, should_tqdm=False, should_cuda=False):
    '''
    Take an optimizer step training the CCER function with soft sampling
    parameterized by a neural network
    Inputs:
    - all_states, the states at each timestep
    - actions, the actions taking at each timestep
    - next_states, the next states after taking actions
    - not dones, N * T array with 0s at final steps and 1s everywhere else
    - rewards, the rewards gained at each timestep
    - q_net, worst-case q neural network
    - q_opt, the optimizer for q_net
    - target_q_net, the target q_net
    - params, dictionary of parameters
    Returns:
    - Loss of the q_net regression problem
    '''
    
    '''Regarding the scalar affixed to the softmax line above (where softmax 'distances' is defined), there appears to be an issue arising from the floating point precision available with pytorch. When the distance values are too big or too small, the differences appear to vanish when softmax is applied, resulting in all equal probabilities. There is likely a better programmatic solution but for now, (1e2,1e3) appears to work best.
            ---
            When normalizing the probability coefficients, it is important that we have a normalization scheme which both 1) sums to 1, to represent probabilities, and 2) delivers the same probability for a (observation, attack) pair on average, regardless of the random sample. This second part is challenging with a small sample size; however, as sample size becomes large, the average softmax probability will approach the true probability'''
    
    discrete = policy_net.discrete
    current_eps = eps_scheduler.get_eps()

    r = range(params.Q_EPOCHS) if not should_tqdm else \
                            tqdm(range(params.Q_EPOCHS))
    for i in r:
        if params.HISTORY_LENGTH > 0 and type(q_net) is ValueLSTMNet: #LSTM Case
            # Create minibatches over episodes, not states
            batches, alive_masks, time_masks, lengths = pack_history([all_states, actions, rewards, not_dones, next_states], not_dones, max_length=params.HISTORY_LENGTH)
            assert not params.SHARE_WEIGHTS
            q_net.lstm.flatten_parameters()
            min_q_net.lstm.flatten_parameters()
            q_opt.zero_grad()

            assert shape_equal_cmp(rewards, not_dones)
            # Minibatch SGD
            for i, batch in enumerate(batches):
                batch_states, batch_actions, batch_rewards, batch_not_dones, batch_next_states = batch
                batch_length = batch_states.shape[0]
                hidden=None
                if hidden is not None:
                    hidden = [h[:, alive_masks[i], :].detach() for h in hidden]
                eps=0.1 #current_eps
                sample_size = 10
                obs_states = batch_states[:, -1,:].clone()
                states_expanded = batch_states.repeat_interleave(dim=0, repeats = sample_size)
                states_sample = ch.cat([Lp_ball(s, epsilon=eps, sample_size=sample_size) for s in obs_states]).view(states_expanded.shape[0], -1)
                states_expanded[:, -1, :] = states_sample
                states_expanded.requires_grad_()


                if params.NORM_MODE == 'value':
                    #P(s') is reflected by the softmax of utility (defined by the value net) of each neighborhood state, given the history
                    actions_expanded = batch_actions.repeat_interleave(dim=0, repeats = sample_size)
                    u, _ = min_q_net.multi_forward(ch.cat((states_expanded, actions_expanded), dim=-1), hidden=hidden)
                    u = u.squeeze(-1).view((batch_actions.shape[0],sample_size,batch_states.shape[-2]))
                    weights_norm = F.softmax(u, dim=-1)
                    
                elif params.NORM_MODE == 'A3B':
                    policy_net.reset()
                    obs_acts, obs_stds, hidden = policy_net.multi_forward(batch_states.repeat_interleave(dim=0, repeats=sample_size), hidden=hidden)
                    belief_acts, belief_stds, _ = policy_net.multi_forward(states_expanded, hidden=hidden)
                    
                    worst_acts = worst_action_pgd(min_q_net, policy_net, states_sample, eps=eps, maxiter=50, return_std=True, not_dones=batch_not_dones)
                    max_kl, _ = KL_dist(worst_acts, (belief_acts[:, -1, :], belief_stds), policy_net, is_action=True).view(-1, sample_size).max(dim=-1) #maximum KL between expected state/actions
                    max_kl = ch.maximum(max_kl.unsqueeze(-1), ch.Tensor([1e-8])) 
                    obs_kl = KL_dist((obs_acts[:, -1, :], obs_stds), (belief_acts[:, -1, :], belief_stds), policy_net, is_action=True).view(-1, sample_size) + 1e-8

                    assert max_kl.shape[-1] == 1
                    assert max_kl.shape[0] == obs_kl.shape[0]

                    likelihood = obs_kl / max_kl #prevent divide-by-zero errors
                    weights_norm = F.softmax(likelihood*1e2, dim=-1).unsqueeze(-1).repeat_interleave(dim=-1, repeats=states_expanded.shape[-2])

                    
                assert not ch.isnan(weights_norm).any() #probably divided by zero

                # soft-worst q prediction of current network given the states.
                hidden=None
                concat_batch_states = ch.cat((batch_states, batch_actions), dim=-1)
                curr_acoe, _ = q_net.multi_forward(concat_batch_states, hidden)
                batch_next_actions, actions_std, _ = policy_net.multi_forward(batch_next_states, hidden)
                concat_states_sample = states_sample.view(-1, sample_size, batch_states.shape[-1]) if discrete else ch.cat((states_expanded, batch_actions.repeat_interleave(dim=0, repeats=sample_size)), dim=-1)
                concat_next_states = batch_next_states if discrete else ch.cat((batch_next_states, batch_next_actions),dim=-1)
                neighborhood_rews, hidden = min_q_net.multi_forward(concat_states_sample, hidden=hidden)
                neighborhood_rews -=  min_q_net.multi_forward(concat_next_states.repeat_interleave(dim=0, repeats=sample_size), hidden=hidden)[0]
                neighborhood_rews = neighborhood_rews.squeeze(-1)
                weighted_rews = ch.sum((batch_rewards.repeat_interleave(dim=0, repeats=sample_size).view(*neighborhood_rews.shape) - neighborhood_rews).view(*weights_norm.shape) * weights_norm, dim=-2)
                expected_acoe = (weighted_rews) + params.GAMMA * batch_not_dones * target_q_net.multi_forward(concat_next_states, hidden=None)[0].squeeze(-1)
                expected_acoe = expected_acoe.unsqueeze(-1)

                q_loss = F.mse_loss(curr_acoe, expected_acoe)
                q_loss.backward()
                q_opt.step()
                soft_update(target_q_net, q_net, params.TAU)

            print(f'acoe_loss={q_loss.item():8.5f}')
            #if i == max(r):
            #    print(f'weights: {weights_norm}')
        else:
            #memoryless policy
            # Create minibatches with shuffling
            state_indices = np.arange(rewards.nelement())
            np.random.shuffle(state_indices)
            splits = np.array_split(state_indices, params.NUM_MINIBATCHES)

            assert shape_equal_cmp(rewards, not_dones)

            # Minibatch SGD
            for selected in splits:
                q_opt.zero_grad()

                def sel(*args):
                    return [v[selected] for v in args]

                def to_cuda(*args):
                    return [v.cuda() for v in args]

                # Get a minibatch (64).
                tup = sel(actions, rewards, not_dones, next_states, all_states)

                if should_cuda: tup = to_cuda(*tup)
                sel_acts, sel_rews, sel_not_dones, sel_next_states, sel_states = tup
                
                eps=0.1 #current_eps
                sample_size = 10
                states_expanded = sel_states.repeat_interleave(dim=0, repeats = sample_size).requires_grad_()
                states_sample = ch.cat([Lp_ball(s, epsilon=eps, sample_size=sample_size) for s in sel_states]).view(*states_expanded.shape)

                distances = KL_dist(states_sample, states_expanded, policy_net)
                
                distances = distances.view(-1, sample_size) + 1e-20 #add a small constant to prevent divide-by-zero


                if params.NORM_MODE == 'p0':
                    weights_norm = F.normalize(distances, p=0)#Lp-normalization
                elif params.NORM_MODE == 'A2B':
                    distance_norm = F.softmax(distances*1e3, dim=-1)#softmax-normalization, i.e. P(s|sampled N) #The scalar here is explained at the top of the function definition
                    atk_lh = F.softmax(ch.sum(distances, dim=1), dim=0) #P(atk|z, N(z)), or likelihood of N(z)|z ### Original line, unused

            
                    weights_norm = (distance_norm) * atk_lh[:, None]
                    assert weights_norm.shape == (sel_rews.shape[0], sample_size)
                elif params.NORM_MODE == 'uniform':
                    distance_norm = distances / ch.sum(distances, dim=-1)[:, None] 
                    atk_lh = 1-(ch.sum(distances, dim=-1) / ch.sum(distances)) #P(atk|z, N(z)), or likelihood of N(z)|z ### Original line, unused

                    weights_norm = (distance_norm) * atk_lh[:, None]                   
                    
                elif params.NORM_MODE == 'A3B':
                    worst_acts = worst_action_pgd(min_q_net, policy_net, states_sample, eps=eps, maxiter=50, return_std=True)
                    expected_acts = policy_net(states_sample)
                    max_kl, _ = KL_dist(worst_acts, expected_acts, policy_net, is_action=True).view(-1, sample_size).max(dim=-1) #maximum KL between expected state/actions
                    max_kl = ch.maximum(max_kl.unsqueeze(-1), ch.Tensor([1e-8])) 
                    obs_kl = KL_dist(states_expanded, states_sample, policy_net, is_action=False).view(-1, sample_size) + 1e-8

                    assert max_kl.shape[-1] == 1
                    assert max_kl.shape[0] == obs_kl.shape[0]

                    likelihood = obs_kl / max_kl #prevent divide-by-zero errors
                    weights_norm = F.softmax(likelihood*1e2, dim=-1)
                    
                assert not ch.isnan(weights_norm).any() #probably divided by zero

                # soft-worst q prediction of current network given the states.
                concat_states = sel_states if discrete else ch.cat((sel_states, sel_acts), dim=1)
                curr_acoe = q_net(concat_states).squeeze(-1)
                next_actions = policy_net(ch.Tensor(sel_next_states))[0]
                concat_states_sample = states_sample.view(-1, sample_size, sel_states.shape[-1]) if discrete else ch.cat((states_sample.view(-1, sample_size, sel_states.shape[-1]), sel_acts.unsqueeze(1).repeat_interleave(dim=1, repeats=sample_size)), dim=-1)
                concat_next_states = sel_next_states if discrete else ch.cat((sel_next_states, next_actions),dim=1)
                neighborhood_rews = min_q_net(concat_states_sample).squeeze(-1)
                neighborhood_rews -=  min_q_net(concat_next_states).repeat_interleave(dim=-1, repeats=sample_size).view(*neighborhood_rews.shape)
                weighted_rews = ch.sum((sel_rews.repeat_interleave(dim=-1, repeats=sample_size).view(*neighborhood_rews.shape) - neighborhood_rews).view(*weights_norm.shape) * weights_norm, dim=-1)
                expected_acoe = (weighted_rews) + params.GAMMA * sel_not_dones * target_q_net(concat_next_states).squeeze(-1)

                q_loss = F.mse_loss(curr_acoe, expected_acoe)
                q_loss.backward()
                q_opt.step()
                soft_update(target_q_net, q_net, params.TAU)

            print(f'acoe_loss={q_loss.item():8.5f}')

    return q_loss

def soft_regret_q_u_step(all_states, actions, next_states, not_dones, rewards, q_net, target_q_net, policy_net, min_q_net, val_net, q_opt, params, eps_scheduler, num_actions, should_tqdm=False, should_cuda=False):
    '''
    Take an optimizer step training the CCER function with soft sampling
    parameterized by a neural network
    Inputs:
    - all_states, the states at each timestep
    - actions, the actions taking at each timestep
    - next_states, the next states after taking actions
    - not dones, N * T array with 0s at final steps and 1s everywhere else
    - rewards, the rewards gained at each timestep
    - q_net, worst-case q neural network
    - q_opt, the optimizer for q_net
    - target_q_net, the target q_net
    - params, dictionary of parameters
    Returns:
    - Loss of the q_net regression problem
    '''
    discrete = policy_net.discrete
    current_eps = eps_scheduler.get_eps()

    r = range(params.Q_EPOCHS) if not should_tqdm else \
                            tqdm(range(params.Q_EPOCHS))
    for i in r:
        # Create minibatches with shuffuling
        state_indices = np.arange(rewards.nelement())
        np.random.shuffle(state_indices)
        splits = np.array_split(state_indices, params.NUM_MINIBATCHES)

        assert shape_equal_cmp(rewards, not_dones)

        # Minibatch SGD
        for selected in splits:
            q_opt.zero_grad()

            def sel(*args):
                return [v[selected] for v in args]

            def to_cuda(*args):
                return [v.cuda() for v in args]

            # Get a minibatch (64).
            tup = sel(actions, rewards, not_dones, next_states, all_states)

            if should_cuda: tup = to_cuda(*tup)
            sel_acts, sel_rews, sel_not_dones, sel_next_states, sel_states = tup
            
            eps=current_eps
            sample_size = 10
            states_expanded = sel_states.repeat_interleave(dim=0, repeats = sample_size)
            states_sample = ch.cat([Lp_ball(s, epsilon=eps, sample_size=sample_size) for s in sel_states]).view(*states_expanded.shape)

            # soft-worst q prediction of current network given the states.
            concat_states = sel_states if discrete else ch.cat((sel_states, sel_acts), dim=1)
            curr_q = q_net(concat_states).squeeze(-1)
            #worst_actions = worst_action_pgd(q_net, policy_net, sel_states, eps=0.01, maxiter=50)
            #worst_next_actions = worst_action_pgd(q_net, policy_net, sel_next_states, eps=0.01, maxiter=50)
            next_actions = policy_net(ch.Tensor(sel_next_states))[0]
            #next_regret = target_q_net(ch.cat((sel_next_states, next_actions),dim=1)).squeeze(-1) - target_q_net(ch.cat((sel_next_states, worst_next_actions), dim=1)).squeeze(-1)
            #expected_q = sel_rews + params.GAMMA * sel_not_dones * next_regret
            concat_states_sample = states_sample.view(-1, sample_size, sel_states.shape[-1]) if discrete else ch.cat((states_sample.view(-1, sample_size, sel_states.shape[-1]), sel_acts.unsqueeze(1).repeat_interleave(dim=1, repeats=sample_size)), dim=-1)
            concat_next_states = sel_next_states if discrete else ch.cat((sel_next_states, next_actions),dim=1)
            neighborhood_rews = min_q_net(concat_states_sample).squeeze(-1)
            neighborhood_rews -=  min_q_net(concat_next_states).repeat_interleave(dim=-1, repeats=sample_size).view(*neighborhood_rews.shape)
            
            
            distances = neighborhood_rews#(KL_dist(states_sample, states_expanded, policy_net)
            distances = distances.view(-1, sample_size) + 1e-20 #add a small constant to prevent divide-by-zero
            if params.NORM_MODE == 'p0':
                weights_norm = F.normalize(distances, p=0)#Lp-normalization
            elif params.NORM_MODE == 'softmax':
                distance_norm = F.softmax(distances*1e3, dim=-1)#softmax-normalization, i.e. P(s|sampled N) #The scalar here is explained below
                #atk_lh = F.softmax(ch.flatten(-distances, 0,-1)*1e3, dim=-1) #P(s|z, N(z)), or likelihood of N(z)|z
                #atk_lh = ch.sum(atk_lh.view(-1, sample_size), dim=-1) #P(s|oracle N)
                atk_lh = F.softmax(ch.sum(distances, dim=1), dim=0) #P(atk|z, N(z)), or likelihood of N(z)|z ### Original line, unused

        
                weights_norm = (distance_norm) * atk_lh[:, None]
                #noatk_prob = torch.ones(size=(distance_norm.shape[0],)) - atk_lh
                assert weights_norm.shape == (sel_rews.shape[0], sample_size)
            elif params.NORM_MODE == 'max':
                distance_norm = distances / ch.sum(distances, dim=-1)[:, None] #softmax-normalization, i.e. P(s|sampled N) #The scalar here is explained below
                #atk_lh = F.softmax(ch.flatten(-distances, 0,-1)*1e3, dim=-1) #P(s|z, N(z)), or likelihood of N(z)|z
                #atk_lh = ch.sum(atk_lh.view(-1, sample_size), dim=-1) #P(s|oracle N)
                atk_lh = 1-(ch.sum(distances, dim=-1) / ch.sum(distances)) #P(atk|z, N(z)), or likelihood of N(z)|z ### Original line, unused

                weights_norm = (distance_norm) * atk_lh[:, None]
            assert not ch.isnan(weights_norm).any() #probably divided by zero
            assert ch.sum(weights_norm, dim=-1).all() == 1 #make sure it fits a probability distribution form
            """
            Regarding the scalar affixed to the softmax line above (where softmax 'distances' is defined), there appears to be an issue arising from the floating point precision available with pytorch. When the distance values are too big or too small, the differences appear to vanish when softmax is applied, resulting in all equal probabilities. There is likely a better programmatic solution but for now, (1e2,1e3) appears to work best.
            ---
            When normalizing the probability coefficients, it is important that we have a normalization scheme which both 1) sums to 1, to represent probabilities, and 2) delivers the same probability for a (observation, attack) pair on average, regardless of the random sample. This second part is challenging with a small sample size; however, as sample size becomes large, the average softmax probability will approach the true probability
            """
            # soft-worst q prediction of current network given the states.
            concat_states = sel_states if discrete else ch.cat((sel_states, sel_acts), dim=1)
            curr_q = q_net(concat_states).squeeze(-1)
            #worst_actions = worst_action_pgd(q_net, policy_net, sel_states, eps=0.01, maxiter=50)
            #worst_next_actions = worst_action_pgd(q_net, policy_net, sel_next_states, eps=0.01, maxiter=50)
            next_actions = policy_net(ch.Tensor(sel_next_states))[0]
            #next_regret = target_q_net(ch.cat((sel_next_states, next_actions),dim=1)).squeeze(-1) - target_q_net(ch.cat((sel_next_states, worst_next_actions), dim=1)).squeeze(-1)
            #expected_q = sel_rews + params.GAMMA * sel_not_dones * next_regret
            concat_states_sample = states_sample.view(-1, sample_size, sel_states.shape[-1]) if discrete else ch.cat((states_sample.view(-1, sample_size, sel_states.shape[-1]), sel_acts.unsqueeze(1).repeat_interleave(dim=1, repeats=sample_size)), dim=-1)
            neighborhood_u = min_q_net(concat_states_sample).squeeze(-1)
            expected_q = (sel_rews) + params.GAMMA * sel_not_dones * val_net(sel_next_states).squeeze(-1)
            weighted_v_u = ch.sum((expected_q.repeat_interleave(dim=-1, repeats=sample_size).view(*weights_norm.shape) - neighborhood_u.view(*weights_norm.shape)).view(*weights_norm.shape) * weights_norm, dim=-1)
            ccer_q = weighted_v_u 
            
            
            q_loss = F.mse_loss(curr_q, ccer_q)
            q_loss.backward()
            q_opt.step()
            soft_update(target_q_net, q_net, params.TAU)

        print(f'V-U_regret_loss={q_loss.item():8.5f}')

    return q_loss

def pack_history(features, not_dones, max_length):
    # Features is a list, each element has dimension (N, state_dim) or (N, ) where N contains a few episodes
    # not_dones splits these episodes (0 in not_dones is end of an episode)
    nnz = ch.nonzero(1.0 - not_dones, as_tuple=False).view(-1).cpu().numpy()
    # nnz has the position where not_dones = 0 (end of episode)
    assert isinstance(features, list)
    # Check dimension. All tensors must have the same dimension.
    size = features[0].size(0)
    for t in features:
        assert size == t.size(0)
    all_pieces = [[] for i in range(len(features))]
    lengths = []
    start = 0
    for i in nnz:
        end = i + 1
        for (a, b) in zip(all_pieces, features):
            a.append(b[start:end])
        lengths.append(end - start)
        start = end
    # The last episode is missing, unless the previous episode end at the last element.
    if end != size:
        for (a, b) in zip(all_pieces, features):
            a.append(b[end:])
        lengths.append(size - end)
    # First pad to longest sequence
    padded_features = [pad_sequence(a, batch_first=True) for a in all_pieces]
    # Then pad to a multiple of max_length
    longest = padded_features[0].size(1)
    extra = int(math.ceil(longest / max_length) * max_length - longest)
    new_padded_features = []
    for t in padded_features:
        if t.ndim == 3:
            new_tensor = ch.zeros(t.size(0), extra, t.size(2))
        else:
            new_tensor = ch.zeros(t.size(0), extra)
        new_tensor = ch.cat([t, new_tensor], dim=1)
        new_padded_features.append(new_tensor)
    del padded_features
    # now divide padded features into chunks with max_length.
    nbatches = new_padded_features[0].size(1) // max_length
    alive_masks = []  # which batch still alives after a chunk
    # time step masks for each chunk, each batch.
    time_masks = []
    batches = [[] for i in range(nbatches)]  # batch of batches
    alive = ch.tensor(lengths)
    alive_iter = ch.tensor(lengths)
    for i in range(nbatches):
        full_mask = alive > 0
        iter_mask = alive_iter > 0
        for t in new_padded_features:
            # only keep the tensors that are alive
            batches[i].append(t[full_mask, i * max_length : i * max_length + max_length])
        # Remove deleted batches
        alive_iter = alive_iter[iter_mask]
        time_mask = alive_iter.view(-1, 1) > ch.arange(max_length).view(1, -1)
        alive -= max_length
        alive_iter -= max_length
        alive_masks.append(iter_mask)
        time_masks.append(time_mask)
    return batches, alive_masks, time_masks, lengths


def ppo_step(all_states, actions, old_log_ps, rewards, returns, not_dones, 
                advs, net, params, store, opt_step):
    '''
    Proximal Policy Optimization
    Runs K epochs of PPO as in https://arxiv.org/abs/1707.06347
    Inputs:
    - all_states, the historical value of all the states
    - actions, the actions that the policy sampled
    - old_log_ps, the log probability of the actions that the policy sampled
    - advs, advantages as estimated by GAE
    - net, policy network to train [WILL BE MUTATED]
    - params, additional placeholder for parameters like EPS
    Returns:
    - The PPO loss; main job is to mutate the net
    '''
    # Storing batches of stuff
    # if store is not None:
    #     orig_dists = net(all_states)

    ### ACTUAL PPO OPTIMIZATION START
    if params.SHARE_WEIGHTS:
        orig_vs = net.get_value(all_states).squeeze(-1).view([params.NUM_ACTORS, -1])
        old_vs = orig_vs.detach()

    """
    print(all_states.size())
    print(actions.size())
    print(old_log_ps.size())
    print(advs.size())
    print(params.HISTORY_LENGTH)
    print(not_dones.size())
    """

    if params.HISTORY_LENGTH > 0:
        # LSTM policy. Need to go over all episodes instead of states.
        # We normalize all advantages at once instead of batch by batch, since each batch may contain different number of samples.
        normalized_advs = adv_normalize(advs)
        batches, alive_masks, time_masks, lengths = pack_history([all_states, actions, old_log_ps, normalized_advs], not_dones, max_length=params.HISTORY_LENGTH)

    for _ in range(params.PPO_EPOCHS):
        if params.HISTORY_LENGTH > 0:
            # LSTM policy. Need to go over all episodes instead of states.
            net.lstm.flatten_parameters()
            params.POLICY_ADAM.zero_grad()
            hidden = None
            surrogate = 0.0
            for i, batch in enumerate(batches):
                # Now we get chunks of time sequences, each of them with a maximum length of params.HISTORY_LENGTH.
                # select log probabilities, advantages of this minibatch.
                batch_states, batch_actions, batch_old_log_ps, batch_advs = batch
                mask = time_masks[i]
                """
                print('batch states', batch_states.size())
                print('batch actions', batch_actions.size())
                print('batch old_log_ps', batch_old_log_ps.size())
                print('batch advs', batch_advs.size())
                print('alive mask', alive_masks[i].size(), alive_masks[i].sum())
                print('mask', mask.size())
                """
                # keep only the alive hidden states.
                if hidden is not None:
                    # print('hidden[0]', hidden[0].size())
                    hidden = [h[:, alive_masks[i], :].detach() for h in hidden]
                    # print('hidden[0]', hidden[0].size())
                # dist contains mean and variance of Gaussian.
                mean, std, hidden = net.multi_forward(batch_states, hidden=hidden)
                dist = mean, std
                # Convert state distribution to log likelyhood.
                new_log_ps = net.get_loglikelihood(dist, batch_actions)
                # print('batch new_log_ps', new_log_ps.size())
                """
                print('old')
                print(batch_old_log_ps)
                print('new')
                print(new_log_ps * mask)
                print('diff')
                print((batch_old_log_ps - new_log_ps * mask).pow(2).sum().item())
                """

                shape_equal_cmp(new_log_ps, batch_old_log_ps)

                # Calculate rewards
                # the surrogate rewards is basically exp(new_log_ps - old_log_ps) * advantage
                # dimension is the same as minibatch size.
                # We already normalized advs before. No need to normalize here.
                unclp_rew = surrogate_reward(batch_advs, new=new_log_ps, old=batch_old_log_ps, mask=mask, normalize=False)
                clp_rew = surrogate_reward(batch_advs, new=new_log_ps, old=batch_old_log_ps,
                                           clip_eps=params.CLIP_EPS, mask=mask, normalize=False)


                # Total loss, is the min of clipped and unclipped reward for each state, averaged.
                surrogate_batch = (-ch.min(unclp_rew, clp_rew) * mask).sum()
                # We sum the batch loss here because each batch contains uneven number of trajactories.
                surrogate = surrogate + surrogate_batch

            # Divide surrogate loss by number of samples in this batch.
            surrogate = surrogate / all_states.size(0)
            # Calculate entropy bonus
            # So far, the entropy only depends on std and does not depend on time. No need to mask.
            entropy_bonus = net.entropies(dist)
            entropy = -params.ENTROPY_COEFF * entropy_bonus
            loss = surrogate + entropy
            # optimizer (only ADAM)
            loss.backward()
            if params.CLIP_GRAD_NORM != -1:
                ch.nn.utils.clip_grad_norm(net.parameters(), params.CLIP_GRAD_NORM)
            params.POLICY_ADAM.step()
        else:
            # Memoryless policy.
            # State is in shape (experience_size, observation_size). Usually 2048.
            state_indices = np.arange(all_states.shape[0])
            np.random.shuffle(state_indices)
            # We use a minibatch of states to do optimization, and each epoch contains several iterations.
            splits = np.array_split(state_indices, params.NUM_MINIBATCHES)
            # A typical mini-batch size is 2048/32=64
            for selected in splits:
                def sel(*args, offset=0):
                    if offset == 0:
                        return [v[selected] for v in args]
                    else:
                        offset_selected = selected + offset
                        return [v[offset_selected] for v in args]

                # old_log_ps: log probabilities of actions sampled based in experience buffer.
                # advs: advantages of these states.
                # both old_log_ps and advs are in shape (experience_size,) = 2048.
                # Using memoryless policy.
                tup = sel(all_states, actions, old_log_ps, advs)
                # select log probabilities, advantages of this minibatch.
                batch_states, batch_actions, batch_old_log_ps, batch_advs = tup
                # print(batch_actions.size())
                # print(batch_advs.size())

                # Forward propagation on current parameters (being constantly updated), to get distribution of these states
                # dist contains mean and variance of Gaussian.
                dist = net(batch_states)
                # print('dist', dist[0].size())
                # print('batch_actions', batch_actions.size())
                # Convert state distribution to log likelyhood.
                new_log_ps = net.get_loglikelihood(dist, batch_actions)
                # print('new_log_ps', new_log_ps.size())
                # print('old_log_ps', batch_old_log_ps.size())

                shape_equal_cmp(new_log_ps, batch_old_log_ps)

                # Calculate rewards
                # the surrogate rewards is basically exp(new_log_ps - old_log_ps) * advantage
                # dimension is the same as minibatch size.
                unclp_rew = surrogate_reward(batch_advs, new=new_log_ps, old=batch_old_log_ps)
                clp_rew = surrogate_reward(batch_advs, new=new_log_ps, old=batch_old_log_ps,
                                           clip_eps=params.CLIP_EPS)

                # Calculate entropy bonus
                # So far, the entropy only depends on std and does not depend on time. No need to mask.
                entropy_bonus = net.entropies(dist).mean()

                # Total loss, is the min of clipped and unclipped reward for each state, averaged.
                surrogate = (-ch.min(unclp_rew, clp_rew)).mean()
                entropy = -params.ENTROPY_COEFF * entropy_bonus
                loss = surrogate + entropy
                
                # If we are sharing weights, take the value step simultaneously 
                # (since the policy and value networks depend on the same weights)
                if params.SHARE_WEIGHTS:
                    tup = sel(returns, not_dones, old_vs)
                    batch_returns, batch_not_dones, batch_old_vs = tup
                    val_loss = value_step(batch_states, batch_returns, batch_advs,
                                          batch_not_dones, net.get_value, None, params,
                                          store, old_vs=batch_old_vs, opt_step=opt_step)
                    loss += params.VALUE_MULTIPLIER * val_loss

                # Optimizer step (Adam or SGD)
                if params.POLICY_ADAM is None:
                    grad = ch.autograd.grad(loss, net.parameters())
                    flat_grad = flatten(grad)
                    if params.CLIP_GRAD_NORM != -1:
                        norm_grad = ch.norm(flat_grad)
                        flat_grad = flat_grad if norm_grad <= params.CLIP_GRAD_NORM else \
                                    flat_grad / norm_grad * params.CLIP_GRAD_NORM

                    assign(flatten(net.parameters()) - params.PPO_LR * flat_grad, net.parameters())
                else:
                    params.POLICY_ADAM.zero_grad()
                    loss.backward()
                    if params.CLIP_GRAD_NORM != -1:
                        ch.nn.utils.clip_grad_norm(net.parameters(), params.CLIP_GRAD_NORM)
                    params.POLICY_ADAM.step()
        print(f'surrogate={surrogate.item():8.5f}, entropy={entropy_bonus.item():8.5f}, loss={loss.item():8.5f}')

    std = ch.exp(net.log_stdev)
    print(f'std_min={std.min().item():8.5f}, std_max={std.max().item():8.5f}, std_mean={std.mean().item():8.5f}')


    return loss.item(), surrogate.item(), entropy.item()


"""Computing an estimated upper bound of KL divergence using SGLD."""
def get_state_kl_bound_sgld(net, batch_states, batch_action_means, eps, steps, stdev, not_dones=None):
    if not_dones is not None:
        # If we have not_dones, the underlying network is a LSTM.
        wrapped_net = functools.partial(net, not_dones=not_dones)
    else:
        wrapped_net = net
    if batch_action_means is None:
        # Not provided. We need to compute them.
        with ch.no_grad():
            batch_action_means, _ = wrapped_net(batch_states)
    else:
        batch_action_means = batch_action_means.detach()
    # upper and lower bounds for clipping
    states_ub = batch_states + eps
    states_lb = batch_states - eps
    step_eps = eps / steps
    # SGLD noise factor. We set (inverse) beta=1e-5 as gradients are relatively small here.
    beta = 1e-5
    noise_factor = np.sqrt(2 * step_eps * beta)
    noise = ch.randn_like(batch_states) * noise_factor
    var_states = (batch_states.clone() + noise.sign() * step_eps).detach().requires_grad_()
    for i in range(steps):
        # Find a nearby state new_phi that maximize the difference
        diff = (wrapped_net(var_states)[0] - batch_action_means) / stdev.detach()
        kl = (diff * diff).sum(axis=-1, keepdim=True).mean()
        # Need to clear gradients before the backward() for policy_loss
        kl.backward()
        # Reduce noise at every step.
        noise_factor = np.sqrt(2 * step_eps * beta) / (i+2)
        # Project noisy gradient to step boundary.
        update = (var_states.grad + noise_factor * ch.randn_like(var_states)).sign() * step_eps
        var_states.data += update
        # clip into the upper and lower bounds
        var_states = ch.max(var_states, states_lb)
        var_states = ch.min(var_states, states_ub)
        var_states = var_states.detach().requires_grad_()
    net.zero_grad()
    diff = (wrapped_net(var_states.requires_grad_(False))[0] - batch_action_means) / stdev
    return (diff * diff).sum(axis=-1, keepdim=True)


def robust_ppo_step(all_states, actions, old_log_ps, rewards, returns, not_dones, 
                advs, net, params, store, opt_step, relaxed_net, eps_scheduler, beta_scheduler):
    '''
    Proximal Policy Optimization with robustness regularizer
    Runs K epochs of PPO as in https://arxiv.org/abs/1707.06347
    Inputs:
    - all_states, the historical value of all the states
    - actions, the actions that the policy sampled
    - old_log_ps, the log probability of the actions that the policy sampled
    - advs, advantages as estimated by GAE
    - net, policy network to train [WILL BE MUTATED]
    - params, additional placeholder for parameters like EPS
    Returns:
    - The PPO loss; main job is to mutate the net
    '''
    # Storing batches of stuff
    # if store is not None:
    #     orig_dists = net(all_states)

    ### ACTUAL PPO OPTIMIZATION START
    if params.SHARE_WEIGHTS:
        orig_vs = net.get_value(all_states).squeeze(-1).view([params.NUM_ACTORS, -1])
        old_vs = orig_vs.detach()

    # We treat all PPO epochs as one epoch.
    eps_scheduler.set_epoch_length(params.PPO_EPOCHS * params.NUM_MINIBATCHES)
    beta_scheduler.set_epoch_length(params.PPO_EPOCHS * params.NUM_MINIBATCHES)
    # We count from 1.
    eps_scheduler.step_epoch()
    beta_scheduler.step_epoch()

    if params.HISTORY_LENGTH > 0:
        # LSTM policy. Need to go over all episodes instead of states.
        # We normalize all advantages at once instead of batch by batch, since each batch may contain different number of samples.
        normalized_advs = adv_normalize(advs)
        batches, alive_masks, time_masks, lengths = pack_history([all_states, actions, old_log_ps, normalized_advs], not_dones, max_length=params.HISTORY_LENGTH)


    for _ in range(params.PPO_EPOCHS):
        if params.HISTORY_LENGTH > 0:
            # LSTM policy. Need to go over all episodes instead of states.
            params.POLICY_ADAM.zero_grad()
            hidden = None
            surrogate = 0.0
            for i, batch in enumerate(batches):
                # Now we get chunks of time sequences, each of them with a maximum length of params.HISTORY_LENGTH.
                # select log probabilities, advantages of this minibatch.
                batch_states, batch_actions, batch_old_log_ps, batch_advs = batch
                mask = time_masks[i]
                """
                print('batch states', batch_states.size())
                print('batch actions', batch_actions.size())
                print('batch old_log_ps', batch_old_log_ps.size())
                print('batch advs', batch_advs.size())
                print('alive mask', alive_masks[i].size(), alive_masks[i].sum())
                print('mask', mask.size())
                """
                # keep only the alive hidden states.
                if hidden is not None:
                    # print('hidden[0]', hidden[0].size())
                    hidden = [h[:, alive_masks[i], :].detach() for h in hidden]
                    # print('hidden[0]', hidden[0].size())
                # dist contains mean and variance of Gaussian.
                mean, std, hidden = net.multi_forward(batch_states, hidden=hidden)
                dist = mean, std
                # Convert state distribution to log likelyhood.
                new_log_ps = net.get_loglikelihood(dist, batch_actions)
                # print('batch new_log_ps', new_log_ps.size())
                """
                print('old')
                print(batch_old_log_ps)
                print('new')
                print(new_log_ps * mask)
                print('diff')
                print((batch_old_log_ps - new_log_ps * mask).pow(2).sum().item())
                """

                shape_equal_cmp(new_log_ps, batch_old_log_ps)

                # Calculate rewards
                # the surrogate rewards is basically exp(new_log_ps - old_log_ps) * advantage
                # dimension is the same as minibatch size.
                # We already normalized advs before. No need to normalize here.
                unclp_rew = surrogate_reward(batch_advs, new=new_log_ps, old=batch_old_log_ps, mask=mask, normalize=False)
                clp_rew = surrogate_reward(batch_advs, new=new_log_ps, old=batch_old_log_ps,
                                           clip_eps=params.CLIP_EPS, mask=mask, normalize=False)


                # Total loss, is the min of clipped and unclipped reward for each state, averaged.
                surrogate_batch = (-ch.min(unclp_rew, clp_rew) * mask).sum()
                # We sum the batch loss here because each batch contains uneven number of trajactories.
                surrogate = surrogate + surrogate_batch

            # Divide surrogate loss by number of samples in this batch.
            surrogate = surrogate / all_states.size(0)
            # Calculate entropy bonus
            # So far, the entropy only depends on std and does not depend on time. No need to mask.
            entropy_bonus = net.entropies(dist)
            # Calculate regularizer under state perturbation.
            eps_scheduler.step_batch()
            beta_scheduler.step_batch()
            batch_action_means = None
            current_eps = eps_scheduler.get_eps()
            stdev = ch.exp(net.log_stdev)
            if params.ROBUST_PPO_DETACH_STDEV:
                # Detach stdev so that it won't be too large.
                stdev = stdev.detach()
            if params.ROBUST_PPO_METHOD == "sgld":
                kl_upper_bound = get_state_kl_bound_sgld(net, all_states, None,
                        eps=current_eps, steps=params.ROBUST_PPO_PGD_STEPS,
                        stdev=stdev, not_dones=not_dones).mean()
            else:
                raise ValueError(f"Unsupported robust PPO method {params.ROBUST_PPO_METHOD}")
            entropy = -params.ENTROPY_COEFF * entropy_bonus
            loss = surrogate + entropy + params.ROBUST_PPO_REG * kl_upper_bound
            # optimizer (only ADAM)
            loss.backward()
            if params.CLIP_GRAD_NORM != -1:
                ch.nn.utils.clip_grad_norm(net.parameters(), params.CLIP_GRAD_NORM)
            params.POLICY_ADAM.step()
        else:
            # Memoryless policy.
            # State is in shape (experience_size, observation_size). Usually 2048.
            state_indices = np.arange(all_states.shape[0])
            np.random.shuffle(state_indices)
            # We use a minibatch of states to do optimization, and each epoch contains several iterations.
            splits = np.array_split(state_indices, params.NUM_MINIBATCHES)
            # A typical mini-batch size is 2048/32=64
            for selected in splits:
                def sel(*args):
                    return [v[selected] for v in args]

                # old_log_ps: log probabilities of actions sampled based in experience buffer.
                # advs: advantages of these states.
                # both old_log_ps and advs are in shape (experience_size,) = 2048.
                tup = sel(all_states, actions, old_log_ps, advs)
                # select log probabilities, advantages of this minibatch.
                batch_states, batch_actions, batch_old_log_ps, batch_advs = tup

                # Forward propagation on current parameters (being constantly updated), to get distribution of these states
                # dist contains mean and variance of Gaussian.
                dist = net(batch_states)
                # Convert state distribution to log likelyhood.
                new_log_ps = net.get_loglikelihood(dist, batch_actions)

                shape_equal_cmp(new_log_ps, batch_old_log_ps)

                # Calculate rewards
                # the surrogate rewards is basically exp(new_log_ps - old_log_ps) * advantage
                # dimension is the same as minibatch size.
                unclp_rew = surrogate_reward(batch_advs, new=new_log_ps, old=batch_old_log_ps)
                clp_rew = surrogate_reward(batch_advs, new=new_log_ps, old=batch_old_log_ps,
                                           clip_eps=params.CLIP_EPS)

                # Calculate entropy bonus
                entropy_bonus = net.entropies(dist).mean()

                # Calculate regularizer under state perturbation.
                eps_scheduler.step_batch()
                beta_scheduler.step_batch()
                batch_action_means = dist[0]
                current_eps = eps_scheduler.get_eps()
                stdev = ch.exp(net.log_stdev)
                if params.ROBUST_PPO_DETACH_STDEV:
                    # Detach stdev so that it won't be too large.
                    stdev = stdev.detach()
                if params.ROBUST_PPO_METHOD == "convex-relax":
                    kl_upper_bound = get_state_kl_bound(relaxed_net, batch_states, batch_action_means,
                            eps=current_eps, beta=beta_scheduler.get_eps(),
                            stdev=stdev).mean()
                elif params.ROBUST_PPO_METHOD == "sgld":
                    kl_upper_bound = get_state_kl_bound_sgld(net, batch_states, batch_action_means,
                            eps=current_eps, steps=params.ROBUST_PPO_PGD_STEPS,
                            stdev=stdev).mean()
                else:
                    raise ValueError(f"Unsupported robust PPO method {params.ROBUST_PPO_METHOD}")

                # Total loss, is the min of clipped and unclipped reward for each state, averaged.
                surrogate = -ch.min(unclp_rew, clp_rew).mean()
                entropy = -params.ENTROPY_COEFF * entropy_bonus
                loss = surrogate + entropy + params.ROBUST_PPO_REG * kl_upper_bound
                
                # If we are sharing weights, take the value step simultaneously 
                # (since the policy and value networks depend on the same weights)
                if params.SHARE_WEIGHTS:
                    tup = sel(returns, not_dones, old_vs)
                    batch_returns, batch_not_dones, batch_old_vs = tup
                    val_loss = value_step(batch_states, batch_returns, batch_advs,
                                          batch_not_dones, net.get_value, None, params,
                                          store, old_vs=batch_old_vs, opt_step=opt_step)
                    loss += params.VALUE_MULTIPLIER * val_loss

                # Optimizer step (Adam or SGD)
                if params.POLICY_ADAM is None:
                    grad = ch.autograd.grad(loss, net.parameters())
                    flat_grad = flatten(grad)
                    if params.CLIP_GRAD_NORM != -1:
                        norm_grad = ch.norm(flat_grad)
                        flat_grad = flat_grad if norm_grad <= params.CLIP_GRAD_NORM else \
                                    flat_grad / norm_grad * params.CLIP_GRAD_NORM

                    assign(flatten(net.parameters()) - params.PPO_LR * flat_grad, net.parameters())
                else:
                    params.POLICY_ADAM.zero_grad()
                    loss.backward()
                    if params.CLIP_GRAD_NORM != -1:
                        ch.nn.utils.clip_grad_norm(net.parameters(), params.CLIP_GRAD_NORM)
                    params.POLICY_ADAM.step()
        # Logging.
        kl_upper_bound = kl_upper_bound.item()
        surrogate = surrogate.item()
        entropy_bonus = entropy_bonus.item()
        print(f'eps={eps_scheduler.get_eps():8.6f}, beta={beta_scheduler.get_eps():8.6f}, kl={kl_upper_bound:10.5g}, '
              f'surrogate={surrogate:8.5f}, entropy={entropy_bonus:8.5f}, loss={loss.item():8.5f}')
    std = ch.exp(net.log_stdev)
    print(f'std_min={std.min().item():8.5f}, std_max={std.max().item():8.5f}, std_mean={std.mean().item():8.5f}')

    if store is not None:
        # TODO: ADV: add row name suffix
        row ={
            'eps': eps_scheduler.get_eps(),
            'beta': beta_scheduler.get_eps(),
            'kl': kl_upper_bound,
            'surrogate': surrogate,
            'entropy': entropy_bonus,
            'loss': loss.item(),
        }
        store.log_table_and_tb('robust_ppo_data', row)

    return loss.item(), surrogate, entropy_bonus

def robust_q_ppo_step(all_states, actions, old_log_ps, rewards, returns, not_dones, 
                advs, net, params, store, opt_step, relaxed_net, eps_scheduler, beta_scheduler, target_q_net,  value_net, n_steps, optional_q_net=None):
    '''
    Proximal Policy Optimization with robustness regularizer
    Runs K epochs of PPO as in https://arxiv.org/abs/1707.06347
    Inputs:
    - all_states, the historical value of all the states
    - actions, the actions that the policy sampled
    - old_log_ps, the log probability of the actions that the policy sampled
    - advs, advantages as estimated by GAE
    - net, policy network to train [WILL BE MUTATED]
    - params, additional placeholder for parameters like EPS
    - optional_q_net: pass in the worst_q_net for ccer-wppo to use alongside the ccer net. For wocar, we just use target_q_net to pass in the worst_q
    Returns:
    - The PPO loss; main job is to mutate the net
    '''
    discrete = net.discrete
    # Storing batches of stuff
    # if store is not None:
    #     orig_dists = net(all_states)

    ### ACTUAL PPO OPTIMIZATION START
    if params.SHARE_WEIGHTS:
        orig_vs = net.get_value(all_states).squeeze(-1).view([params.NUM_ACTORS, -1])
        old_vs = orig_vs.detach()

    # We treat all PPO epochs as one epoch.
    eps_scheduler.set_epoch_length(params.PPO_EPOCHS * params.NUM_MINIBATCHES)
    beta_scheduler.set_epoch_length(params.PPO_EPOCHS * params.NUM_MINIBATCHES)
    # We count from 1.
    eps_scheduler.step_epoch()
    beta_scheduler.step_epoch()

    if params.WEIGHT_SCHEDULE == 'constant':
        q_weight = params.Q_WEIGHT
    elif params.WEIGHT_SCHEDULE == 'linear':
        q_weight = (params.Q_WEIGHT/params.TRAIN_STEPS)  * float(n_steps)
    elif params.WEIGHT_SCHEDULE == 'exp':
        q_weight = math.pow((params.Q_WEIGHT/params.TRAIN_STEPS) * float(n_steps), 3)
    elif params.WEIGHT_SCHEDULE == 'stage':
        if n_steps < params.TRAIN_STEPS/2:
            q_weight = 0
        else:
            q_weight = params.Q_WEIGHT
    elif params.WEIGHT_SCHEDULE == 'stage-linear':
        stage_steps = (params.Q_WEIGHT/0.8)  * float(params.TRAIN_STEPS)
        if n_steps <= stage_steps:
            q_weight = (params.Q_WEIGHT/stage_steps)  * float(n_steps)
        else:
            q_weight = params.Q_WEIGHT
    else:
        print("Unsupported weight schedule")
    if q_weight > 1:
        q_weight = 1

    if params.HISTORY_LENGTH > 0:
        # LSTM policy. Need to go over all episodes instead of states.
        # We normalize all advantages at once instead of batch by batch, since each batch may contain different number of samples.
        normalized_advs = adv_normalize(advs)
        batches, alive_masks, time_masks, lengths = pack_history([all_states, actions, old_log_ps, normalized_advs], not_dones, max_length=params.HISTORY_LENGTH)


    for _ in range(params.PPO_EPOCHS):
        if params.HISTORY_LENGTH > 0:
            # LSTM policy. Need to go over all episodes instead of states.
            params.POLICY_ADAM.zero_grad()
            net.lstm.flatten_parameters()
            hidden = None
            surrogate = 0.0
            for i, batch in enumerate(batches):
                # Now we get chunks of time sequences, each of them with a maximum length of params.HISTORY_LENGTH.
                # select log probabilities, advantages of this minibatch.
                batch_states, batch_actions, batch_old_log_ps, batch_advs = batch
                if net.discrete:
                    batch_actions = batch_actions[:, 0]
                mask = time_masks[i]
                batch_robust_q = target_q_net.multi_forward(ch.cat((batch_states, batch_actions), dim=-1), None)[0].squeeze(-1)
                if optional_q_net:
                    batch_min_q = target_q_net.multi_forward(ch.cat((batch_states, batch_actions), dim=-1), None)[0].squeeze(-1)
                batch_value = value_net.multi_forward(batch_states, None)[0].squeeze(-1)
                '''
                print('batch states', batch_states.size())
                print('batch actions', batch_actions.size())
                print('batch old_log_ps', batch_old_log_ps.size())
                print('batch advs', batch_advs.size())
                print('alive mask', alive_masks[i].size(), alive_masks[i].sum())
                print('mask', mask.size())
                print('worst_q', batch_worst_q.size())
                '''
                # keep only the alive hidden states.
                if hidden is not None:
                    # print('hidden[0]', hidden[0].size())
                    hidden = [h[:, alive_masks[i], :].detach() for h in hidden]
                    # print('hidden[0]', hidden[0].size())
                # dist contains mean and variance of Gaussian.
                mean, std, hidden = net.multi_forward(batch_states, hidden=hidden)
                dist = mean, std
                # Convert state distribution to log likelyhood.
                new_log_ps = net.get_loglikelihood(dist, batch_actions)
                # print('batch new_log_ps', new_log_ps.size())
                """
                print('old')
                print(batch_old_log_ps)
                print('new')
                print(new_log_ps * mask)
                print('diff')
                print((batch_old_log_ps - new_log_ps * mask).pow(2).sum().item())
                """
                shape_equal_cmp(new_log_ps, batch_old_log_ps)

                # Calculate rewards
                # the surrogate rewards is basically exp(new_log_ps - old_log_ps) * advantage
                # dimension is the same as minibatch size.
                # We already normalized advs before. No need to normalize here.
                is_ccer = (params.ccer or params.acoe or params.ccer_wppo or params.soarl)
                if params.ccer_wppo:#using wppo, so we need both min_reward and ccer in the surrogate advantage
                    use_wocar = batch_min_q
                unclp_rew = surrogate_adv_q(batch_advs, new=new_log_ps, old=batch_old_log_ps, ccer=batch_robust_q, q_weight=q_weight, mask=mask, normalize=True, regret_q=is_ccer, use_wocar=None)
                clp_rew = surrogate_adv_q(batch_advs, new=new_log_ps, old=batch_old_log_ps, ccer=batch_robust_q, q_weight=q_weight, clip_eps=params.CLIP_EPS, mask=mask, normalize=True, regret_q=is_ccer, use_wocar=None)

                # Total loss, is the min of clipped and unclipped reward for each state, averaged.
                surrogate_batch = (-ch.min(unclp_rew, clp_rew) * mask).sum()
                # We sum the batch loss here because each batch contains uneven number of trajactories.
                surrogate = surrogate + surrogate_batch

            # Divide surrogate loss by number of samples in this batch.
            surrogate = surrogate / all_states.size(0)
            # Calculate entropy bonus
            # So far, the entropy only depends on std and does not depend on time. No need to mask.
            entropy_bonus = net.entropies(dist)
            # Calculate regularizer under state perturbation.
            eps_scheduler.step_batch()
            beta_scheduler.step_batch()
            batch_action_means = None
            current_eps = eps_scheduler.get_eps()
            stdev = ch.exp(net.log_stdev)
            if params.ROBUST_PPO_DETACH_STDEV:
                # Detach stdev so that it won't be too large.
                stdev = stdev.detach()
            if params.ROBUST_PPO_METHOD == "sgld":
                kl_upper_bound = get_state_kl_bound_sgld(net, all_states, None,
                        eps=current_eps, steps=params.ROBUST_PPO_PGD_STEPS,
                        stdev=stdev, not_dones=not_dones).mean()
            else:
                raise ValueError(f"Unsupported robust PPO method {params.ROBUST_PPO_METHOD}")
            entropy = -params.ENTROPY_COEFF * entropy_bonus 
            loss = surrogate + entropy + params.ROBUST_PPO_REG * kl_upper_bound
            # optimizer (only ADAM)
            loss.backward()
            if params.CLIP_GRAD_NORM != -1:
                ch.nn.utils.clip_grad_norm(net.parameters(), params.CLIP_GRAD_NORM)
            params.POLICY_ADAM.step()
        else:
            # Memoryless policy.
            # State is in shape (experience_size, observation_size). Usually 2048.
            state_indices = np.arange(all_states.shape[0])
            np.random.shuffle(state_indices)
            # We use a minibatch of states to do optimization, and each epoch contains several iterations.
            splits = np.array_split(state_indices, params.NUM_MINIBATCHES)
            # A typical mini-batch size is 2048/32=64
            for selected in splits:
                def sel(*args):
                    return [v[selected] for v in args]

                # old_log_ps: log probabilities of actions sampled based in experience buffer.
                # advs: advantages of these states.
                # both old_log_ps and advs are in shape (experience_size,) = 2048.
                tup = sel(all_states, actions, old_log_ps, advs)
                # select log probabilities, advantages of this minibatch.
                batch_states, batch_actions, batch_old_log_ps, batch_advs = tup
                if discrete:
                    batch_actions = batch_actions[:, 0]
                concat_batch_states = batch_states if discrete else ch.cat((batch_states, batch_actions), dim=1)
                batch_robust_q = target_q_net(concat_batch_states).squeeze(-1)
                batch_value = value_net(batch_states).squeeze(-1)
                '''
                print('batch states', batch_states.size())
                print('batch actions', batch_actions.size())
                print('batch old_log_ps', batch_old_log_ps.size())
                print('batch advs', batch_advs.size())
                '''
                # Forward propagation on current parameters (being constantly updated), to get distribution of these states
                # dist contains mean and variance of Gaussian.
                dist = net(batch_states)
                # Convert state distribution to log likelyhood.
                new_log_ps = net.get_loglikelihood(dist, batch_actions)

                shape_equal_cmp(new_log_ps, batch_old_log_ps)

                # print('advs:', batch_advs)
                # print('worst_q', batch_worst_q)
                # Calculate rewards
                # the surrogate rewards is basically exp(new_log_ps - old_log_ps) * advantage
                # dimension is the same as minibatch size.
                is_ccer = (params.ccer or params.acoe or params.ccer_wppo or params.soarl or params.soft_q_u_regret)
                unclp_rew = surrogate_adv_q(batch_advs, new=new_log_ps, old=batch_old_log_ps, ccer=batch_robust_q, q_weight=q_weight, regret_q=is_ccer, acoe_advs=params.acoe_advs)
                clp_rew = surrogate_adv_q(batch_advs, new=new_log_ps, old=batch_old_log_ps, ccer=batch_robust_q, q_weight=q_weight, clip_eps=params.CLIP_EPS, regret_q=is_ccer, acoe_advs=params.acoe_advs)

                # Calculate entropy bonus
                entropy_bonus = net.entropies(dist).mean()

                # Calculate regularizer under state perturbation.
                eps_scheduler.step_batch()
                beta_scheduler.step_batch()
                batch_action_means = dist[0]
                current_eps = eps_scheduler.get_eps()
                stdev = ch.exp(net.log_stdev)
                if params.ROBUST_PPO_DETACH_STDEV:
                    # Detach stdev so that it won't be too large.
                    stdev = stdev.detach()
                if params.ROBUST_PPO_METHOD == "convex-relax":
                    kl_upper_bound = get_state_kl_bound(relaxed_net, batch_states, batch_action_means,
                            eps=current_eps, beta=beta_scheduler.get_eps(),
                            stdev=stdev).mean()
                elif params.ROBUST_PPO_METHOD == "sgld":
                    kl_upper_bound = get_state_kl_bound_sgld(net, batch_states, batch_action_means,
                            eps=current_eps, steps=params.ROBUST_PPO_PGD_STEPS,
                            stdev=stdev).mean()
                else:
                    raise ValueError(f"Unsupported robust PPO method {params.ROBUST_PPO_METHOD}")

                # Total loss, is the min of clipped and unclipped reward for each state, averaged.
                surrogate = -ch.min(unclp_rew, clp_rew).mean()
                entropy = -params.ENTROPY_COEFF * entropy_bonus 
                loss = surrogate + entropy + params.ROBUST_PPO_REG * kl_upper_bound
                
                # If we are sharing weights, take the value step simultaneously 
                # (since the policy and value networks depend on the same weights)
                if params.SHARE_WEIGHTS:
                    tup = sel(returns, not_dones, old_vs)
                    batch_returns, batch_not_dones, batch_old_vs = tup
                    val_loss = value_step(batch_states, batch_returns, batch_advs,
                                          batch_not_dones, net.get_value, None, params,
                                          store, old_vs=batch_old_vs, opt_step=opt_step)
                    loss += params.VALUE_MULTIPLIER * val_loss

                # Optimizer step (Adam or SGD)
                if params.POLICY_ADAM is None:
                    grad = ch.autograd.grad(loss, net.parameters())
                    flat_grad = flatten(grad)
                    if params.CLIP_GRAD_NORM != -1:
                        norm_grad = ch.norm(flat_grad)
                        flat_grad = flat_grad if norm_grad <= params.CLIP_GRAD_NORM else \
                                    flat_grad / norm_grad * params.CLIP_GRAD_NORM

                    assign(flatten(net.parameters()) - params.PPO_LR * flat_grad, net.parameters())
                else:
                    params.POLICY_ADAM.zero_grad()
                    loss.backward()
                    if params.CLIP_GRAD_NORM != -1:
                        ch.nn.utils.clip_grad_norm(net.parameters(), params.CLIP_GRAD_NORM)
                    params.POLICY_ADAM.step()
        # Logging.
        kl_upper_bound = kl_upper_bound.item()
        surrogate = surrogate.item()
        entropy_bonus = entropy_bonus.item()
        robust_q = batch_robust_q.mean().item()
        curr_vs = batch_value.mean().item()
        print(f'eps={eps_scheduler.get_eps():8.6f}, beta={beta_scheduler.get_eps():8.6f}, kl={kl_upper_bound:10.5g}, '
              f'surrogate={surrogate:8.5f}, entropy={entropy_bonus:8.5f}, loss={loss.item():8.5f}, robust_q={robust_q:8.5f}, curr_vs={curr_vs:8.5f}')
    std = ch.exp(net.log_stdev)
    print(f'std_min={std.min().item():8.5f}, std_max={std.max().item():8.5f}, std_mean={std.mean().item():8.5f}')
    print(f'robust_q_weight={q_weight:8.5f}')
    if store is not None:
        # TODO: ADV: add row name suffix
        row ={
            'eps': eps_scheduler.get_eps(),
            'beta': beta_scheduler.get_eps(),
            'kl': kl_upper_bound,
            'surrogate': surrogate,
            'entropy': entropy_bonus,
            'loss': loss.item(),
            'robust_q': robust_q,
            'robust_q_weight': q_weight,
            'current_value': curr_vs
        }
        store.log_table_and_tb('robust_q_ppo_data', row)

    return loss.item(), surrogate, entropy_bonus



def trpo_step(all_states, actions, old_log_ps, rewards, returns, not_dones, advs, net, params, store, opt_step):
    '''
    Trust Region Policy Optimization
    Runs K epochs of TRPO as in https://arxiv.org/abs/1502.05477
    Inputs:
    - all_states, the historical value of all the states
    - actions, the actions that the policy sampled
    - old_log_ps, the probability of the actions that the policy sampled
    - advs, advantages as estimated by GAE
    - net, policy network to train [WILL BE MUTATED]
    - params, additional placeholder for parameters like EPS
    Returns:
    - The TRPO loss; main job is to mutate the net
    '''    
    # Initial setup
    initial_parameters = flatten(net.parameters()).clone()
    # all_states is in shape (experience_size, observation_size). Usually 2048 experiences.
    # Get mean and std of action distribution for all experiences.
    pds = net(all_states)
    # And compute the log probabilities for the actions chosen at rollout time.
    action_log_probs = net.get_loglikelihood(pds, actions)

    # Calculate losses
    surr_rew = surrogate_reward(advs, new=action_log_probs, old=old_log_ps).mean()
    grad = ch.autograd.grad(surr_rew, net.parameters(), retain_graph=True)
    # This represents the computation of gradient, and will be used to obtain 2nd order.
    flat_grad = flatten(grad)

    # Make fisher product estimator. Only use a fraction of examples.
    num_samples = int(all_states.shape[0] * params.FISHER_FRAC_SAMPLES)
    selected = np.random.choice(range(all_states.shape[0]), num_samples, replace=False)
    
    detached_selected_pds = select_prob_dists(pds, selected, detach=True)
    selected_pds = select_prob_dists(pds, selected, detach=False)
    
    # Construct the KL divergence which we will optimize on. This is essentially 0, but what we care about is the Hessian.
    # We want to know when the network parameter changes, how the K-L divergence of network output changes.
    kl = net.calc_kl(detached_selected_pds, selected_pds).mean()
    # g is the gradient of the KL divergence w.r.t to parameters. It is 0 at the starting point.
    g = flatten(ch.autograd.grad(kl, net.parameters(), create_graph=True))
    '''
    Fisher matrix to vector x product. Essentially, a Hessian-vector product of K-L divergence w.r.t network parameter.
    '''
    def fisher_product(x, damp_coef=1.):
        contig_flat = lambda q: ch.cat([y.contiguous().view(-1) for y in q])
        # z is the gradient-vector product. Take the derivation of it to get Hessian vector product.
        z = g @ x
        hv = ch.autograd.grad(z, net.parameters(), retain_graph=True)
        return contig_flat(hv).detach() + x*params.DAMPING * damp_coef

    # Find KL constrained gradient step
    # The Fisher matrix A is unknown, but we can compute the product.
    # flat_grad is the right-hand side value b. Want to solve x in Ax = b
    step = cg_solve(fisher_product, flat_grad, params.CG_STEPS)
    # Return the solution. "step" has size of network parameters.

    max_step_coeff = (2 * params.MAX_KL / (step @ fisher_product(step)))**(0.5)
    max_trpo_step = max_step_coeff * step

    if store and params.SHOULD_LOG_KL:
        kl_approximation_logging(all_states, pds, flat_grad, step, net, store)
        kl_vs_second_order_approx(all_states, pds, net, max_trpo_step, params, store, opt_step)

    # Backtracking line search
    with ch.no_grad():
        # Backtracking function, which gives the improvement on objective given an update direction s.
        def backtrack_fn(s):
            assign(initial_parameters + s.data, net.parameters())
            test_pds = net(all_states)
            test_action_log_probs = net.get_loglikelihood(test_pds, actions)
            new_reward = surrogate_reward(advs, new=test_action_log_probs, old=old_log_ps).mean()
            # surr_new is the surrogate before optimization.
            # We need to make sure the loss is improving, and KL between old probabilites are not too large.
            if params.TRPO_KL_REDUCE_FUNC == 'mean':
                kl_metric = net.calc_kl(pds, test_pds).mean()
            elif params.TRPO_KL_REDUCE_FUNC == 'max':
                kl_metric = net.calc_kl(pds, test_pds).max()
            else:
                raise ValueError("unknown reduce function " + params.TRPO_KL_REDUCE_FUNC)
            if new_reward <= surr_rew or kl_metric > params.MAX_KL:
                return -float('inf')
            return new_reward - surr_rew
        expected_improve = flat_grad @ max_trpo_step
        # max_trpo_step is the search direction. Backtracking line search will find a scaler for it.
        # expected_improve is the expected decrease in loss estimated by gradient.
        # backtracking_line_search will try a scaler 0.5, 0.25, 0.125, etc to achieve expected improvement.
        final_step = backtracking_line_search(backtrack_fn, max_trpo_step,
                                              expected_improve,
                                              num_tries=params.MAX_BACKTRACK)

        assign(initial_parameters + final_step, net.parameters())

    # entropy regularization not used for TRPO so return 0.
    return surr_rew.item(), 0.0, 0.0

def step_with_mode(mode, adversary=False):
    STEPS = {
        'trpo': trpo_step,
        'ppo': ppo_step,
        'robust_ppo': robust_ppo_step,
        'adv_ppo': ppo_step,
        'adv_pa_ppo': robust_ppo_step,
        'adv_trpo': trpo_step,
        'adv_sa_ppo': robust_ppo_step,
        'robust_q_ppo': robust_q_ppo_step,
        'adv_acoe_ppo': robust_q_ppo_step,
        'adv_destruct_ppo': robust_q_ppo_step,
        'adv_iterative_ppo': ppo_step,

    }
    ADV_STEPS = {
        'trpo': None,
        'ppo': None,
        'robust_ppo': None,
        'robust_q_ppo': None,
        'adv_ppo': ppo_step,
        'adv_pa_ppo': ppo_step,
        'adv_trpo': trpo_step,
        'adv_sa_ppo': ppo_step,
        'adv_acoe_ppo': ppo_step,
        'adv_destruct_ppo': ppo_step,
        'adv_iterative_ppo': ppo_step,
    }
    if adversary:
        return ADV_STEPS[mode]
    else:
        return STEPS[mode]


def get_params_norm(net, p=2):
    layer_norms = []
    layer_norms_dict = {}
    for name, params in net.named_parameters():
        if name != 'log_stdev' and name != 'log_weight' and params.ndim != 1:
            norm = ch.norm(params.view(-1), p=p).item() / np.prod(params.size())
            layer_norms.append(norm)
            layer_norms_dict[name] = norm
    return np.array(layer_norms), layer_norms_dict


last_norm = None

