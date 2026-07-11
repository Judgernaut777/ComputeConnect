"""ComputeConnect — the compute plane of the Connect family.

Two API layers, one backend (ARCHITECTURE §5):

* Layer 1, control plane: the six ``LocalComputeProvider`` routes AgentConnect's
  shipped client already calls.
* Layer 2, inference: an OpenAI-compatible ``/v1/chat/completions`` surface.

Privacy is structural (ARCHITECTURE §6): cloud candidates are removed before
placement, and an empty candidate set is a structured refusal — never a silent
downgrade.
"""

__version__ = "0.1.0"
