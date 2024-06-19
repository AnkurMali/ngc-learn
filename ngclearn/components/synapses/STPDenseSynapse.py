from jax import random, numpy as jnp, jit
from ngclearn import resolver, Component, Compartment
from ngclearn.components.synapses import DenseSynapse
from ngclearn.utils import tensorstats
from ngclearn.utils.weight_distribution import initialize_params
from ngcsimlib.logger import info

class STPDenseSynapse(DenseSynapse): ## short-term plastic synaptic cable
    """
    A dynamic dense synaptic cable; this synapse evolves according to
    short-term plasticity (STP) dynamics.

    | --- Synapse Compartments: ---
    | inputs - input (takes in external signals)
    | outputs - output
    | weights - current value matrix of synaptic efficacies
    | biases - current value vector of synaptic bias values
    | --- Short-Term Plasticity Compartments: ---
    | resources - fixed value matrix of synaptic resources (U)
    | u - release probability; fraction of resources ready for use
    | x - fraction of resources available after neurotransmitter depletion

    | Dynamics note:
    | If tau_d >> tau_f and resources U are large, then synapse is STD-dominated
    | If tau_d << tau_f and resources U are small, then synases is STF-dominated

    Args:
        name: the string name of this cell

        shape: tuple specifying shape of this synaptic cable (usually a 2-tuple
            with number of inputs by number of outputs)

        weight_init: a kernel to drive initialization of this synaptic cable's values;
            typically a tuple with 1st element as a string calling the name of
            initialization to use

        bias_init: a kernel to drive initialization of biases for this synaptic cable
            (Default: None, which turns off/disables biases)

        resist_scale: a fixed (resistance) scaling factor to apply to synaptic
            transform (Default: 1.), i.e., yields: out = ((W * Rscale) * in)

        p_conn: probability of a connection existing (default: 1.); setting
            this to < 1 and > 0. will result in a sparser synaptic structure
            (lower values yield sparse structure)

        tau_f: short-term facilitation time constant (default: 750 ms)

        tau_d: shoft-term depression time constant (default: 50 ms)

        resources_int: initialization kernel for synaptic resources matrix
    """

    # Define Functions
    def __init__(self, name, shape, weight_init=None, bias_init=None,
                 resist_scale=1., p_conn=1., tau_f=750., tau_d=50.,
                 resources_init=None, **kwargs):
        super().__init__(name, shape, weight_init, bias_init, resist_scale,
                         p_conn, **kwargs)
        ## STP meta-parameters
        self.resources_init = resources_init
        self.tau_f = tau_f
        self.tau_d = tau_d

        ## Set up short-term plasticity / dynamic synapse compartment values
        tmp_key, *subkeys = random.split(self.key.value, 4)
        preVals = jnp.zeros((self.batch_size, shape[0]))
        self.u = Compartment(preVals) ## release prob variables
        self.x = Compartment(preVals) ## resource availability variables
        if self.resources_init is None:
            info(self.name, "is using default resources value initializer!")
            self.weight_init = {"dist": "uniform", "amin": 0.125, "amax": 0.175} # 0.15
        self.resources = Compartment(
            initialize_params(subkeys[2], resources_init, shape)
        ) ## matrix U - synaptic resources matrix

    @staticmethod
    def _advance_state(tau_f, tau_d, Rscale, inputs, weights, biases, resources, u, x):
        weights_dyn = weights
        '''
        ## u and x: both column vectors of length = num pre-syn nodes
        u = [u - u/tau_f + U * (1. - u)] * s # + (1. - s) * (u - u/tau_f) ## STF
        W = W_full * u * x * s ## synaptic current change
        x = [x + (1. - x)/tau_d - u * x] * s #+ (1. - s) * (x + (1. - x)/tau_d) ## STD
        '''
        s = inputs
        ## compute short-term facilitation
        u = jnp.sum(u - u * (1./tau_f) + (resources * (1. - u)) * s, axis=0, keepdims=True)
        ## compute dynamic synaptic values/conductances
        weights_dyn = weights_dyn * u * x ## OR: -W/tau_w + W * u * x
        if tau_d > 0.:
            ## compute short-term depression
            x = x + (1. - x) * (1./tau_d) - u * x * s
        outputs = jnp.matmul(inputs, weights_dyn * Rscale) + biases
        return outputs, u, x

    @resolver(_advance_state)
    def advance_state(self, outputs, u, x):
        self.outputs.set(outputs)
        self.u.set(u)
        self.x.set(x)

    @staticmethod
    def _reset(batch_size, shape):
        preVals = jnp.zeros((batch_size, shape[0]))
        postVals = jnp.zeros((batch_size, shape[1]))
        inputs = preVals
        outputs = postVals
        u0 = preVals
        x0 = preVals
        return inputs, outputs, u0, x0

    @resolver(_reset)
    def reset(self, inputs, outputs, u, x):
        self.inputs.set(inputs)
        self.outputs.set(outputs)
        self.u.set(u)
        self.x.set(x)

    def save(self, directory, **kwargs):
        file_name = directory + "/" + self.name + ".npz"
        if self.bias_init != None:
            jnp.savez(file_name,
                      weights=self.weights.value,
                      biases=self.biases.value,
                      resources=self.resources.value)
        else:
            jnp.savez(file_name,
                      weights=self.weights.value,
                      resources=self.resources.value)

    def load(self, directory, **kwargs):
        file_name = directory + "/" + self.name + ".npz"
        data = jnp.load(file_name)
        self.weights.set(data['weights'])
        self.resources.set(data['resources'])
        if "biases" in data.keys():
            self.biases.set(data['biases'])

    def help(self): ## component help function
        properties = {
            "synapse_type": "DenseSynapse - performs a synaptic transformation of inputs to produce "
                            "output signals (e.g., a scaled linear multivariate transformation); "
                            "this synapse is dynamic, adapting via a form of short-term plasticity"
        }
        compartment_props = {
            "input_compartments":
                {"inputs": "Takes in external input signal values",
                 "key": "JAX RNG key"},
            "parameter_compartments":
                {"weights": "Synapse efficacy/strength parameter values",
                 "biases": "Base-rate/bias parameter values",
                 "resources": "Synaptic resource paramter values (U)",
                 "u": "Release probability variables",
                 "x": "Resource depletion variables"},
            "outputs_compartments":
                {"outputs": "Output of synaptic transformation"},
        }
        hyperparams = {
            "shape": "Shape of synaptic weight value matrix; number inputs x number outputs",
            "weight_init": "Initialization conditions for synaptic weight (W) values",
            "bias_init": "Initialization conditions for bias/base-rate (b) values",
            "resist_scale": "Resistance level scaling factor (applied to output of transformation)",
            "p_conn": "Probability of a connection existing (otherwise, it is masked to zero)",
            "tau_f": "Short-term facilitation time constant",
            "tau_d": "Short-term depression time constant"
        }
        info = {self.name: properties,
                "compartments": compartment_props,
                "dynamics": "outputs = [(W * Rscale) * inputs] + b; "
                            "dW/dt = W_full * u * x * inputs",
                "hyperparameters": hyperparams}
        return info

    def __repr__(self):
        comps = [varname for varname in dir(self) if Compartment.is_compartment(getattr(self, varname))]
        maxlen = max(len(c) for c in comps) + 5
        lines = f"[{self.__class__.__name__}] PATH: {self.name}\n"
        for c in comps:
            stats = tensorstats(getattr(self, c).value)
            if stats is not None:
                line = [f"{k}: {v}" for k, v in stats.items()]
                line = ", ".join(line)
            else:
                line = "None"
            lines += f"  {f'({c})'.ljust(maxlen)}{line}\n"
        return lines

if __name__ == '__main__':
    from ngcsimlib.context import Context
    with Context("Bar") as bar:
        Wab = STPDenseSynapse("Wab", (2, 3))
    print(Wab)
