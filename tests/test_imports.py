"""Import everything to check coverage and dependencies."""

import importlib
import pkgutil

import PyFlowBatt


class TestImportAllModules:
    """Import all modules."""

    def test_import_all_modules(self) -> None:
        """Dynamically import all modules in the pyflowbatt package."""
        package = PyFlowBatt
        for _importer, modname, _ispkg in pkgutil.walk_packages(
            package.__path__,
            package.__name__ + ".",
        ):
            importlib.import_module(modname)
