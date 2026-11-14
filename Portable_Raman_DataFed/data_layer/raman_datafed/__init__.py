"""Data layer of the Portable Raman controller, DataFed edition.

Registers captured spectra in a catalog (DataFed, or a local demo stand-in), uploads the raw
files, and keeps a local ledger so that captures made offline are registered later.
The contract every module follows is data_layer/PROTOCOL.md.
"""

__version__ = "1.0.0"
