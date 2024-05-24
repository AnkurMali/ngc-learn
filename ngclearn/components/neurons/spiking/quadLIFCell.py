from ngcsimlib.component import Component
from ngcsimlib.compartment import Compartment
from ngcsimlib.resolver import resolver
from ngclearn.components.neurons.spiking.LIFCell import LIFCell ## import parent cell class/component
from jax import numpy as jnp, random, jit, nn
from functools import partial
import time, sys
from ngclearn.utils.diffeq.ode_utils import get_integrator_code, \
                                            step_euler, step_rk2

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

@jit
def _modify_current(j, dt, tau_m): ## electrical current re-scaling co-routine
    jScale = tau_m/dt
    return j * jScale

@jit
def _dfv_internal(j, v, rfr, tau_m, refract_T, v_rest, v_c, a0): ## raw voltage dynamics
    mask = (rfr >= refract_T).astype(jnp.float32) # get refractory mask
    ## update voltage / membrane potential
    dv_dt = ((v_rest - v) * (v - v_c) * a0) + (j * mask)
    dv_dt = dv_dt * (1./tau_m)
    return dv_dt

def _dfv(t, v, params): ## voltage dynamics wrapper
    j, rfr, tau_m, refract_T, v_rest, v_c, a0 = params
    dv_dt = _dfv_internal(j, v, rfr, tau_m, refract_T, v_rest, v_c, a0)
    return dv_dt

@partial(jit, static_argnums=[7,8,9,10,11,12,13,14])
def run_cell(dt, j, v, v_thr, v_theta, rfr, skey, v_c, a0, tau_m, R_m, v_rest,
             v_reset, refract_T, integType=0):
    """
    Runs quadratic leaky integrator neuronal dynamics

    Args:
        dt: integration time constant (milliseconds, or ms)

        j: electrical current value

        v: membrane potential (voltage, in milliVolts or mV) value (at t)

        v_thr: base voltage threshold value (in mV)

        v_theta: threshold shift (homeostatic) variable (at t)

        rfr: refractory variable vector (one per neuronal cell)

        skey: PRNG key which, if not None, will trigger a single-spike constraint
            (i.e., only one spike permitted to emit per single step of time);
            specifically used to randomly sample one of the possible action
            potentials to be an emitted spike

        v_c: scaling factor for voltage accumulation

        a0: critical voltage value

        tau_m: cell membrane time constant

        R_m: membrane resistance value

        v_rest: membrane resting potential (in mV)

        v_reset: membrane reset potential (in mV) -- upon occurrence of a spike,
            a neuronal cell's membrane potential will be set to this value

        refract_T: (relative) refractory time period (in ms; Default
            value is 1 ms)

        integType: integer indicating type of integration to use

    Returns:
        voltage(t+dt), spikes, raw spikes, updated refactory variables
    """
    _v_thr = v_theta + v_thr ## calc present voltage threshold
    mask = (rfr >= refract_T).astype(jnp.float32) # get refractory mask
    ## update voltage / membrane potential (v_c ~> 0.8?) (a0 usually <1?)
    #_v = v + ((v_rest - v) * (v - v_c) * a0) * (dt/tau_m) + (j * mask)
    v_params = (j, rfr, tau_m, refract_T, v_rest, v_c, a0)
    if integType == 1:
        _, _v = step_rk2(0., v, _dfv, dt, v_params)
    else: #_v = v + (v_rest - v) * (dt/tau_m) + (j * mask)
        _, _v = step_euler(0., v, _dfv, dt, v_params)
    ## obtain action potentials
    s = (_v > _v_thr).astype(jnp.float32)
    ## update refractory variables
    _rfr = (rfr + dt) * (1. - s)
    ## perform hyper-polarization of neuronal cells
    _v = _v * (1. - s) + s * v_reset

    raw_s = s + 0 ## preserve un-altered spikes
    ############################################################################
    ## this is a spike post-processing step
    if skey is not None: ## FIXME: this would not work for mini-batches!!!!!!!
        m_switch = (jnp.sum(s) > 0.).astype(jnp.float32)
        rS = random.choice(skey, s.shape[1], p=jnp.squeeze(s))
        rS = nn.one_hot(rS, num_classes=s.shape[1], dtype=jnp.float32)
        s = s * (1. - m_switch) + rS * m_switch
    ############################################################################
    return _v, s, raw_s, _rfr

@partial(jit, static_argnums=[3,4])
def update_theta(dt, v_theta, s, tau_theta, theta_plus=0.05):
    """
    Runs homeostatic threshold update dynamics one step.

    Args:
        dt: integration time constant (milliseconds, or ms)

        v_theta: current value of homeostatic threshold variable

        s: current spikes (at t)

        tau_theta: homeostatic threshold time constant

        theta_plus: physical increment to be applied to any threshold value if
            a spike was emitted

    Returns:
        updated homeostatic threshold variable
    """
    theta_decay = jnp.exp(-dt/tau_theta)
    _v_theta = v_theta * theta_decay + s * theta_plus
    return _v_theta

class QuadLIFCell(LIFCell): ## quadratic (leaky) LIF cell; inherits from LIFCell
    """
    A spiking cell based on quadratic leaky integrate-and-fire (LIF) neuronal
    dynamics. Note that QuadLIFCell is a child of LIFCell and inherits its
    main set of routines, only overriding its dynamics in advance().

    Dynamics can be taken to be governed by the following ODE:

    | d.Vz/d.t = a0 * (V - V_rest) * (V - V_c) + Jz * R) * (dt/tau_mem)

    where:

    |   a0 - scaling factor for voltage accumulation
    |   V_c - critical voltage (value)

    Args:
        name: the string name of this cell

        n_units: number of cellular entities (neural population size)

        tau_m: membrane time constant

        R_m: membrane resistance value

        thr: base value for adaptive thresholds that govern short-term
            plasticity (in milliVolts, or mV)

        v_rest: membrane resting potential (in mV)

        v_reset: membrane reset potential (in mV) -- upon occurrence of a spike,
            a neuronal cell's membrane potential will be set to this value

        v_scale: scaling factor for voltage accumulation (v_c)

        critical_V: critical voltage value (a0)

        tau_theta: homeostatic threshold time constant

        theta_plus: physical increment to be applied to any threshold value if
            a spike was emitted

        refract_T: relative refractory period time (ms; Default: 1 ms)

        one_spike: if True, a single-spike constraint will be enforced for
            every time step of neuronal dynamics simulated, i.e., at most, only
            a single spike will be permitted to emit per step -- this means that
            if > 1 spikes emitted, a single action potential will be randomly
            sampled from the non-zero spikes detected

        key: PRNG key to control determinism of any underlying random values
            associated with this cell

        useVerboseDict: triggers slower, verbose dictionary mode (Default: False)

        directory: string indicating directory on disk to save LIF parameter
            values to (i.e., initial threshold values and any persistent adaptive
            threshold values)
    """

    # Define Functions
    def __init__(self, name, n_units, tau_m, R_m, thr=-52., v_rest=-65., v_reset=60.,
                 v_c=-41.6, a0=1., tau_theta=1e7, theta_plus=0.05, refract_T=5.,
                 key=None, one_spike=True, directory=None, **kwargs):
        super().__init__(name, n_units, tau_m, R_m, thr, v_rest, v_reset,
                         tau_theta, theta_plus, refract_T, key, one_spike,
                         directory, **kwargs)
        ## only two distinct additional constants distinguish the Quad-LIF cell
        self.v_c = v_scale
        self.a0 = critical_V

    @staticmethod
    def _advance_state(t, dt, tau_m, R_m, v_rest, v_reset, refract_T, tau_theta,
                 theta_plus, one_spike, v_c, a0, key, j, v, s, rfr, thr,
                 thr_theta, tols):
        skey = None ## this is an empty dkey if single_spike mode turned off
        if one_spike == True: ## old code ~> if self.one_spike is False:
            key, *subkeys = random.split(key, 2)
            skey = subkeys[0]
        ## run one integration step for neuronal dynamics
        j = _modify_current(j, dt, tau_m)
        v, s, raw_spikes, rfr = run_cell(dt, j, v, thr, thr_theta, rfr, skey,
                                         v_c, a0, tau_m, R_m, v_rest, v_reset,
                                         refract_T)
        if tau_theta > 0.:
            ## run one integration step for threshold dynamics
            thr_theta = update_theta(dt, thr_theta, raw_spikes, tau_theta, theta_plus)
        ## update tols
        tols = update_times(t, s, tols)
        return j, v, s, rfr, thr, thr_theta, tols, key

    @resolver(_advance_state)
    def advance_state(self, j, v, s, rfr, thr, thr_theta, tols, key):
        self.j.set(j)
        self.v.set(v)
        self.s.set(s)
        self.rfr.set(rfr)
        self.thr.set(thr)
        self.thr_theta.set(thr_theta)
        self.tols.set(tols)
        self.key.set(key)
