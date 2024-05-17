from ngcsimlib.component import Component
from ngcsimlib.compartment import Compartment
from ngcsimlib.resolver import resolver

from jax import numpy as jnp, random, jit
from functools import partial
import time, sys
from ngclearn.utils.diffeq.ode_utils import get_integrator_code, \
                                            step_euler, step_rk2
from ngclearn.utils.surrogate_fx import secant_lif_estimator

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

@partial(jit, static_argnums=[3,4])
def modify_current(j, spikes, inh_weights, R_m, inh_R):
    """
    A simple function that modifies electrical current j via application of a
    scalar membrane resistance value and an approximate form of lateral inhibition.
    Note that if no inhibitory resistance is set (i.e., inh_R = 0), then no
    lateral inhibition is applied. Functionally, this routine carries out the
    following piecewise equation:

    | j * R_m - [Wi * s(t-dt)] * inh_R, if inh_R > 0
    | j * R_m, otherwise

    Args:
        j: electrical current value

        spikes: previous binary spike vector (for t-dt)

        inh_weights: lateral recurrent inhibitory synapses (typically should be
            chosen to be a scaled hollow matrix)

        R_m: membrane resistance (to multiply/scale j by)

        inh_R: inhibitory resistance to scale lateral inhibitory current by; if
            inh_R = 0, NO lateral inhibitory pressure will be applied

    Returns:
        modified electrical current value
    """
    _j = j * R_m
    if inh_R > 0.:
        _j = _j - (jnp.matmul(spikes, inh_weights) * inh_R)
    return _j

## co-routines for run_cell
# @partial(jit, static_argnums=[4,5,6])
# def _update_voltage(dt, j, v, rfr, tau_m, refract_T, v_min=None):
#     mask = (rfr >= refract_T).astype(jnp.float32) # get refractory mask
#     _v = (v + (-v + j) * (dt / tau_m)) * mask
#     if v_min is not None:
#         _v = jnp.maximum(v_min, _v)
#     #_v = v + (-v) * (dt / tau_m) + j * mask
#     return _v, mask

@jit
def _dfv_internal(j, v, rfr, tau_m, refract_T): ## raw voltage dynamics
    mask = (rfr >= refract_T).astype(jnp.float32) # get refractory mask
    #dv_dt = ((-v + j) * (dt / tau_m)) * mask
    dv_dt = (-v + j)
    dv_dt = dv_dt * (1./tau_m) * mask
    return dv_dt

def _dfv(t, v, params): ## voltage dynamics wrapper
    j, rfr, tau_m, refract_T = params
    dv_dt = _dfv_internal(j, v, rfr, tau_m, refract_T)
    return dv_dt

@jit
def _hyperpolarize(v, s):
    _v = (1. - s) * v ## hyper-polarize cells
    return _v

@partial(jit, static_argnums=[3,4,5])
def _update_threshold(dt, v_thr, spikes, thrGain=0.002, thrLeak=0.0005, rho_b = 0.):
    ## update thresholds if applicable
    if rho_b > 0.: ## run sparsity-enforcement threshold
        dthr = jnp.sum(spikes, axis=1, keepdims=True) - 1.0
        _v_thr = jnp.maximum(v_thr + dthr * rho_b, 0.025)
    else: ## run simple adaptive threshold
        thr_gain = spikes * thrGain
        thr_leak = (v_thr * thrLeak)
        _v_thr = v_thr + thr_gain - thr_leak
    return _v_thr

@partial(jit, static_argnums=[4])
def _update_refract_and_spikes(dt, rfr, s, refract_T, sticky_spikes=False):
    mask = (rfr >= refract_T).astype(jnp.float32) ## Note: wasted repeated compute
    ## update refractory variables
    _rfr = (rfr + dt) * (1. - s) + s * dt # set refract to dt
    _s = s
    if sticky_spikes == True: ## pin refractory spikes if configured
        _s = s * mask + (1. - mask)
    return _rfr, _s

def run_cell(dt, j, v, v_thr, tau_m, rfr, spike_fx, refract_T=1., thrGain=0.002,
             thrLeak=0.0005, rho_b = 0., sticky_spikes=False, v_min=None):
    """
    Runs leaky integrator neuronal dynamics

    Args:
        dt: integration time constant (milliseconds, or ms)

        j: electrical current value

        v: membrane potential (voltage) value (at t)

        v_thr: voltage threshold value (at t)

        tau_m: cell membrane time constant

        rfr: refractory variable vector (one per neuronal cell)

        spike_fx: spike emission function of form `spike_fx(v, v_thr)`

        refract_T: (relative) refractory time period (in ms; Default
            value is 1 ms)

        thrGain: the amount of threshold incremented per time step (if spike present)

        thrLeak: the amount of threshold value leaked per time step

        rho_b: sparsity factor; if > 0, will force adaptive threshold to operate
            with sparsity across a layer enforced

        sticky_spikes: if True, then spikes are pinned at value of action potential
            (i.e., 1) for as long as the relative refractory occurs (this recovers
            the source paper's core spiking process)

    Returns:
        voltage(t+dt), spikes, threshold(t+dt), updated refactory variables
    """
    #new_voltage, mask = _update_voltage(dt, j, v, rfr, tau_m, refract_T, v_min)
    v_params = (j, rfr, tau_m, refract_T)
    _, _v = step_euler(0., v, _dfv, dt, v_params) #_v = step_euler(v, v_params, _dfv, dt)
    # if v_min is not None:
    #     _v = jnp.maximum(v_min, _v)
    spikes = spike_fx(_v, v_thr)
    _v = _hyperpolarize(_v, spikes)
    new_thr = _update_threshold(dt, v_thr, spikes, thrGain, thrLeak, rho_b)
    _rfr, spikes = _update_refract_and_spikes(dt, rfr, spikes, refract_T, sticky_spikes)
    return _v, spikes, new_thr, _rfr

class SLIFCell(Component): ## leaky integrate-and-fire cell
    """
    A spiking cell based on a simplified leaky integrate-and-fire (sLIF) model.
    This neuronal cell notably contains functionality required by the computational
    model employed by (Samadi et al., 2017, i.e., a surrogate derivative function
    and "sticky spikes") as well as the additional incorporation of an adaptive
    threshold (per unit) scheme. (Note that this particular spiking cell only
    supports Euler integration of its voltage dynamics.)

    | Reference:
    | Samadi, Arash, Timothy P. Lillicrap, and Douglas B. Tweed. "Deep learning with
    | dynamic spiking neurons and fixed feedback weights." Neural computation 29.3
    | (2017): 578-602.

    Args:
        name: the string name of this cell

        n_units: number of cellular entities (neural population size)

        tau_m: membrane time constant

        R_m: membrane resistance value

        thr: base value for adaptive thresholds (initial condition for
            per-cell thresholds) that govern short-term plasticity

        inhibit_R: lateral modulation factor (DEFAULT: 6.); if >0, this will trigger
            a heuristic form of lateral inhibition via an internally integrated
            hollow matrix multiplication

        thr_persist: are adaptive thresholds persistent? (Default: False)

            :Note: depending on the value of this boolean variable:
                True = adaptive thresholds are NEVER reset upon call to reset
                False = adaptive thresholds are reset to "thr" upon call to reset

        thrGain: how much adaptive thresholds increment by

        thrLeak: how much adaptive thresholds are decremented/decayed by

        refract_T: relative refractory period time (ms; Default: 1 ms)

        rho_b: threshold sparsity factor (Default: 0)

        sticky_spikes: if True, spike variables will be pinned to action potential
            value (i.e, 1) throughout duration of the refractory period; this recovers
            a key setting used by Samadi et al., 2017

        thr_jitter: scale of uniform jitter to add to initialization of thresholds

        key: PRNG key to control determinism of any underlying random values
            associated with this cell

        useVerboseDict: triggers slower, verbose dictionary mode (Default: False)

        directory: string indicating directory on disk to save sLIF parameter
            values to (i.e., initial threshold values and any persistent adaptive
            threshold values)
    """

    # Define Functions
    def __init__(self, name, n_units, tau_m, R_m, thr, inhibit_R=0., thr_persist=False,
                 thrGain=0.0, thrLeak=0.0, rho_b=0., refract_T=0., sticky_spikes=False,
                 thr_jitter=0.05, key=None, useVerboseDict=False, directory=None, **kwargs):
        super().__init__(name, useVerboseDict, **kwargs)

        key = random.PRNGKey(time.time_ns()) if key is None else key

        ## membrane parameter setup (affects ODE integration)
        self.tau_m = tau_m ## membrane time constant
        self.R_m = R_m ## resistance value
        self.refract_T = refract_T #5. # 2. ## refractory period  # ms
        self.v_min = -3.
        ## variable below determines if spikes pinned at 1 during refractory period?
        self.sticky_spikes = sticky_spikes

        ## set up surrogate function for spike emission
        self.spike_fx, self.d_spike_fx = secant_lif_estimator()

        ## create simple recurrent inhibitory pressure
        self.inh_R = inhibit_R ## lateral inhibitory magnitude
        key, subkey = random.split(key)
        self.inh_weights = random.uniform(subkey, (n_units, n_units), minval=0.025, maxval=1.)
        MV = 1. - jnp.eye(n_units)
        self.inh_weights = self.inh_weights * MV

        ##Layer Size Setup
        self.n_units = n_units
        self.batch_size = 1

        ## adaptive threshold setup
        self.rho_b = rho_b
        self.thr_persist = thr_persist ## are adapted thresholds persistent? True (persistent)
        self.thrGain = thrGain #0.0005
        self.thrLeak = thrLeak #0.00005
        if directory is None:
            self.thr_jitter =  thr_jitter ## some random jitter to ensure thresholds start off different
            key, subkey = random.split(key)
            self.threshold0 = thr + random.uniform(subkey, (1, n_units),
                                                  minval=-self.thr_jitter, maxval=self.thr_jitter,
                                                  dtype=jnp.float32)
        else:
            self.load(directory)

        ## Compartments
        self.key = Compartment(key)
        self.j = Compartment() ## electrical current, input
        self.s = Compartment(jnp.zeros((self.batch_size, self.n_units))) ## spike/action potential, output
        self.tols = Compartment() ## time-of-last-spike (record vector)
        self.v = Compartment(jnp.zeros((self.batch_size, self.n_units))) ## membrane potential/voltage
        self.thr = Compartment(self.threshold0 + 0) ## action potential threshold
        self.rfr = Compartment(jnp.zeros((self.batch_size, self.n_units)) + self.refract_T) ## refractory variable(s)
        self.surrogate = Compartment() ## surrogate signal

    # def verify_connections(self):
    #     self.metadata.check_incoming_connections(self.inputCompartmentName(), min_connections=1)

    @staticmethod
    def pure_advance(t, dt, inh_weights, R_m, inh_R, d_spike_fx, tau_m, spike_fx, refract_T,
                    thrGain, thrLeak, rho_b, sticky_spikes, v_min,  j, s, v, thr, rfr, tols):
        ## run one step of Euler integration over neuronal dynamics
        j_curr = j
        ## apply simplified inhibitory pressure
        j_curr = modify_current(j_curr, s, inh_weights, R_m, inh_R)
        j = j_curr # None ## store electrical current
        surrogate = d_spike_fx(j_curr, c1=0.82, c2=0.08)
        v, s, thr, rfr = \
            run_cell(dt, j_curr, v, thr, tau_m,
                     rfr, spike_fx, refract_T, thrGain, thrLeak,
                     rho_b, sticky_spikes=sticky_spikes, v_min=v_min)
        ## update tols
        tols = update_times(t, s, tols)
        return j, s, tols, v, thr, rfr, surrogate

    @resolver(pure_advance, output_compartments=['j', 's', 'tols', 'v', 'thr', 'rfr', 'surrogate'])
    def advance(self, j, s, tols, v, thr, rfr, surrogate):
        self.j.set(j)
        self.s.set(s)
        self.tols.set(tols)
        self.thr.set(thr)
        self.rfr.set(rfr)
        self.surrogate.set(surrogate)
        self.v.set(v)

    def reset(self, **kwargs):
        self.voltage = jnp.zeros((self.batch_size, self.n_units))
        self.refract = jnp.zeros((self.batch_size, self.n_units)) + self.refract_T
        self.current = None
        self.surrogate = None
        self.timeOfLastSpike = jnp.zeros((self.batch_size, self.n_units))
        self.spikes = jnp.zeros((self.batch_size, self.n_units))
        if self.thr_persist == False: ## if thresh non-persistent, reset to base value
            self.threshold = self.threshold0 + 0




    def save(self, directory, **kwargs):
        file_name = directory + "/" + self.name + ".npz"
        if self.thr_persist == False:
            jnp.savez(file_name, threshold=self.threshold0)
        else:
            jnp.savez(file_name, threshold=self.thr.value)

    def load(self, directory, **kwargs):
        file_name = directory + "/" + self.name + ".npz"
        data = jnp.load(file_name)
        self.thr.set(data['threshold'])
        self.threshold0 = self.threshold + 0


if __name__ == '__main__':
    from ngcsimlib.compartment import All_compartments
    from ngcsimlib.context import Context
    from ngcsimlib.commands import Command
