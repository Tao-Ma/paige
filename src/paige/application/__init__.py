"""Application services — pure orchestrators over ports.

Services in this layer compose the ports + domain to do useful
work. They MUST NOT import `paige.adapters` (the architecture's
contract), and MUST NOT import `paige.testing`. Tests inject
fakes that satisfy the port Protocols.
"""
