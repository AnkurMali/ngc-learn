import sys
import subprocess
import pkg_resources
from pkg_resources import get_distribution
#from pathlib import Path
#from sys import argv

__version__ = get_distribution('ngclearn').version

#required = {'ngcsimlib', 'jax', 'jaxlib'} ## list of core ngclearn dependencies
required = {'ngcsimlib', 'jax', 'jaxlib'}
installed = {pkg.key for pkg in pkg_resources.working_set}
missing = required - installed

for key in required:
    if key in missing:
        raise ImportError(str(key) + ", a core dependency of ngclearn, is not " \
                          "currently installed!")


## Needed to preload is called before anything in ngclearn
from pathlib import Path
from sys import argv

import ngcsimlib

from ngcsimlib.context import Context
from ngcsimlib.component import Component
from ngcsimlib.compartment import Compartment
from ngcsimlib.resolver import resolver


from ngcsimlib import configure, preload_modules
from ngcsimlib import logger

if not Path(argv[0]).name == "sphinx-build" or Path(argv[0]).name == "build.py":
    if "readthedocs" not in argv[0]:  ## prevent readthedocs execution of preload
        configure()
        logger.init_logging()
        preload_modules()