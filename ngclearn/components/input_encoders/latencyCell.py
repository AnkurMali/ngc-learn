from ngcsimlib.component import Component
from ngcsimlib.resolver import resolver
from ngcsimlib.compartment import Compartment

from ngclearn.utils.model_utils import clamp_min, clamp_max
from jax import numpy as jnp, random, jit
from functools import partial
import time

@jit
def update_times(t, s, tols):
    """
    Updates time-of-last-spike (tols) variable.

    Args:
        t: current time (a scalar/int value)

        s: binary spike vector

        tols: current time-of-last-spike variable

    Returns:
        updated tols variable
    """
    _tols = (1. - s) * tols + (s * t)
    return _tols

@partial(jit, static_argnums=[5])
def calc_spike_times_linear(data, tau, thr, first_spk_t, num_steps=1.,
                            normalize=False):
    """
    Computes spike times from data according to a linear latency encoding scheme.

    Args:
        data: pattern data to convert to spikes/times

        tau: latency coding time constant

        thr: latency coding threshold value

        first_spk_t: first spike time(s) (either int or vector
            with same shape as spk_times; in ms)

        num_steps: number of total time steps of simulation to consider

        normalize: normalize the logarithmic latency code values (uses num_steps)

    Returns:
        projected spike times
    """
    _tau = tau
    if normalize == True:
        _tau = num_steps - 1. - first_spk_t ## linear normalization
    #torch.clamp_max((-tau * (data - 1)), -tau * (threshold - 1))
    stimes = -_tau * (data - 1.) ## calc raw latency code values
    max_bound = -_tau * (thr - 1.) ## upper bound latency code values
    stimes = clamp_max(stimes, max_bound) ## apply upper bound
    return stimes + first_spk_t

@partial(jit, static_argnums=[6])
def calc_spike_times_nonlinear(data, tau, thr, first_spk_t, eps=1e-7,
                               num_steps=1., normalize=False):
    """
    Computes spike times from data according to a logarithmic encoding scheme.

    Args:
        data: pattern data to convert to spikes/times

        tau: latency coding time constant

        thr: latency coding threshold value

        first_spk_t: first spike time(s) (either int or vector
            with same shape as spk_times; in ms)

        eps: small numerical error control factor (added to thr)

        num_steps: number of total time steps of simulation to consider

        normalize: normalize the logarithmic latency code values (uses num_steps)

    Returns:
        projected spike times
    """
    _data = clamp_min(data, thr + eps) # saturates all values below threshold.
    stimes = jnp.log(_data / (_data - thr)) * tau ## calc spike times
    stimes = stimes + first_spk_t

    if normalize == True:
        term1 = (stimes - first_spk_t)
        term2 = (num_steps - first_spk_t - 1.)
        term3 = jnp.max(stimes - first_spk_t)
        stimes = term1 * (term2 / term3) + first_spk_t
    return stimes

@jit
def extract_spike(spk_times, t, mask):
    """
    Extracts a spike from a latency-coded spike train.

    Args:
        spk_times: spike times to compare against

        t: current time

        mask: prior spike mask (1 if spike has occurred, 0 otherwise)

    Returns:
        binary spikes, boolean mask to indicate if spikes have occurred as of yet
    """
    _spk_times = jnp.round(spk_times) # snap times to nearest integer time
    spikes_t = (_spk_times <= t).astype(jnp.float32) # get spike
    spikes_t = spikes_t * (1. - mask)
    _mask = mask + (1. - mask) * spikes_t
    return spikes_t, _mask

class LatencyCell(Component):
    """
    A (nonlinear) latency encoding (spike) cell; produces a time-lagged set of
    spikes on-the-fly.

    Args:
        name: the string name of this cell

        n_units: number of cellular entities (neural population size)

        tau: time constant for model used to calculate firing time (Default: 1 ms)

        threshold: sensory input features below this threhold value will fire at
            final step in time of this latency coded spike train

        first_spike_time: time of first allowable spike (ms) (Default: 0 ms)

        linearize: should the linear latency encoding scheme be used? (otherwise,
            defaults to logarithmic latency encoding)

        normalize: normalize the latency code such that final spike(s) occur
            a pre-specified number of simulation steps "num_steps"? (Default: False)

            :Note: if this set to True, you will need to choose a useful value
                for the "num_steps" argument (>1), depending on how many steps simulated

        num_steps: number of discrete time steps to consider for normalized latency
            code (only useful if "normalize" is set to True) (Default: 1)

        key: PRNG key to control determinism of any underlying synapses
            associated with this cell

        useVerboseDict: triggers slower, verbose dictionary mode (Default: False)
    """

    # Define Functions
    def __init__(self, name, n_units, tau=1., threshold=0.01, first_spike_time=0.,
                 linearize=False, normalize=False, num_steps=1., key=None,
                 useVerboseDict=False, **kwargs):
        super().__init__(name, useVerboseDict, **kwargs)

        ##Random Number Set up
        self.key = key
        if self.key is None:
            self.key = random.PRNGKey(time.time_ns())

        self.first_spike_time = first_spike_time
        self.tau = tau
        self.threshold = threshold
        self.linearize = linearize
        ## normalize latency code s.t. final spike(s) occur w/in num_steps
        self.normalize = normalize
        self.num_steps = num_steps

        self.target_spike_times = None
        self._mask = None

        ##Layer Size Setup
        self.batch_size = 1
        self.n_units = n_units

        ## Compartment setup
        self.inputs = Compartment(None) # input compartment
        self.outputs = Compartment(jnp.zeros((self.batch_size, self.n_units))) # output compartment
        self.tols = Compartment(jnp.zeros((self.batch_size, self.n_units))) # time of last spike
        self.key = Compartment(random.PRNGKey(time.time_ns()) if key is None else key)
        self.targ_sp_times = Compartment(jnp.zeros((self.batch_size, self.n_units)))
        #self.reset()

    @staticmethod
    def pure_calc_spike_times(t, dt, linearize, tau, threshold, first_spike_time,
        num_steps, normalize, inputs):
        ## would call this function before processing a spike train (at start)
        data = inputs
        if linearize == True: ## linearize spike time calculation
            stimes = calc_spike_times_linear(data, tau, threshold,
                                             first_spike_time,
                                             num_steps, normalize)
            targ_sp_times = stimes #* calcEvent + targ_sp_times * (1. - calcEvent)
        else: ## standard nonlinear spike time calculation
            stimes = calc_spike_times_nonlinear(data, tau, threshold,
                                                first_spike_time,
                                                num_steps=num_steps,
                                                normalize=normalize)
            targ_sp_times = stimes #* calcEvent + targ_sp_times * (1. - calcEvent)
        return targ_sp_times

    @resolver(pure_calc_spike_times, output_compartments=['targ_sp_times'])
    def calc_spike_times(self, vals):
        targ_sp_times = vals
        self.targ_sp_times.set(targ_sp_times)

    @staticmethod
    def pure_advance(t, dt, key, inputs, mask, targ_sp_times, tols):
        key, *subkeys = random.split(key, 2)
        data = inputs ## get sensory pattern data / features
        # if targ_sp_times == None: ## calc spike times if not called yet
        #     if linearize == True: ## linearize spike time calculation
        #         stimes = calc_spike_times_linear(data, tau, threshold,
        #                                          first_spike_time,
        #                                          num_steps, normalize)
        #         targ_sp_times = stimes
        #     else: ## standard nonlinear spike time calculation
        #         stimes = calc_spike_times_nonlinear(data, tau, threshold,
        #                                             first_spike_time,
        #                                             num_steps=num_steps,
        #                                             normalize=normalize)
        #         targ_sp_times = stimes
        #spk_mask = mask
        spikes, spk_mask = extract_spike(targ_sp_times, t, mask) ## get spikes at t
        return spikes, tols, spk_mask, targ_sp_times, key

    @resolver(pure_advance, output_compartments=['outputs', 'tols', 'mask',
        'targ_sp_times', 'key'])
    def advance(self, vals):
        outputs, tols, mask, targ_sp_times, key = vals
        self.outputs.set(outputs)
        self.tols.set(tols)
        self.mask.set(mask)
        self.targ_sp_times.set(targ_sp_times)
        self.key.set(key)

    @staticmethod
    def pure_reset(batch_size, n_units):
        return (None, jnp.zeros((batch_size, n_units)),
               jnp.zeros((batch_size, n_units)),
               jnp.zeros((batch_size, n_units)),
               jnp.zeros((batch_size, n_units)))

    @resolver(pure_reset, output_compartments=['inputs', 'outputs', 'tols',
        'mask', 'targ_sp_times',])
    def reset(self, inputs, outputs, tols, mask):
        self.inputs.set(inputs)
        self.outputs.set(outputs)
        self.tols.set(tols)
        self.mask.set(mask)
        self.targ_sp_times.set(targ_sp_times)

    def save(self, **kwargs):
        pass
