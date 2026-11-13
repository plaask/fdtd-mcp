# -*- coding: utf-8 -*-
"""Bridge-wide constants (runs on Lumerical's bundled Python 3.6.8)."""
from __future__ import print_function
import re

ERR_MSG = 300          # cap for error-message strings
TRUNC = 1000           # default per-array element cap for sanitise

# User-property type codes accepted by adduserprop / addanalysisprop.
# Verified live on v202: only the INTEGER form works ('Length' is rejected).
LSF_PROP_TYPES = {'number': 0, 'text': 1, 'length': 2, 'time': 3,
                  'frequency': 4, 'material': 5}

# LSF commands that start a solve; executing any of these on an unsaved project
# pops an invisible 'Save As' dialog that wedges the hidden engine.
RUN_COMMAND = re.compile(
    r'(?<![A-Za-z_])(run|runsweep|runanalysis|runsweepanalysis|runmany|resume)\b')
