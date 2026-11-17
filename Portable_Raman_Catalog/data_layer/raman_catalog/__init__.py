"""Data layer of the Portable Raman controller, catalog edition.

Registers captured spectra in a catalog kept on this Mac (the local demo catalog), stores the raw
files, and keeps a local ledger so that captures made offline are registered later.
The contract every module follows is data_layer/PROTOCOL.md.
"""

__version__ = "1.0.0"
