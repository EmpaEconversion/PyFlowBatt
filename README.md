# 🌊 PyFlowBatt

This Python package contains functions and a CLI for analysing data from the flow battery lab at Empa.


## Installation

Use Python >3.10, and install with pip:
```
git clone https://github.com/EmpaEconversion/PyFlowBatt.git

cd PyFlowBatt

pip install .
```

## Usage

Use in a terminal (e.g. Command Prompt or PowerShell).

To analyse data and get a full summary and ro-crate, use `pyflowbatt --folder="path/to/your/folder"`.

You can also navigate to a folder e.g.
```
cd path/to/your/folder
pyflowbatt
```

You can do a 'dry run' (nothing is actually analysed) with `pyflowbatt --dry` or `pyflowbatt --dry --folder="path/to/your/folder"`.

PyFlowBatt will automatically scan for files like GCPL, EIS, LSV, CV, as well as BattInfo Converter .xlsx files, process the data, convert to battery data format, produce summary graphs and excel reports, create a BattInfo JSON-LD metadata file, and a top level ro-crate metadata file.

You can override some settings by adding a `pyflowbatt.toml` file to a sample folder, parent folder, or your home folder. Use `pyflowbatt init` to create a template file and see what can be modified.

## For contributors

If you find bugs, or want new features, open an *issue* on the GitHub page.

If you want to directly contribute to the project, first clone the repo and install it as editable with developer dependencies:
```
git clone https://github.com/empaeconversion/pyflowbatt.git

cd pyflowbatt

pip install -e .[dev]
```
Then open this folder in an editor like VSCode.

You should always create a new branch before making edits, `main` is only for tested, working code that everyone can use.

E.g. make and switch to a new branch with
```
git checkout -b new-feature
```

You have now branched off the `main` PyFlowBatt into your own branch called e.g. `new-feature`. Now you can do whatever you like and it won't affect the `main` branch.

When you have made and committed some changes that add a feature or fix a problem, and you want to put them into `main`, open a *pull request* on GitHub.
