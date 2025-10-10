# 🌊 flussli

This Python package contains functions and a CLI for analysing data from the flow battery lab at Empa.


## Installation

Use Python >3.10, and install with pip:
```
git clone https://github.com/empaeconversion/flussli.git

cd flussli

pip install .
```

## Usage

Use in a terminal (e.g. Command Prompt or PowerShell), and check options with:
```
flussli --help
```

You can navigate to a sample folder, or folder containing multiple sample folders, and run `flussli`.

You can also use `flussli --folder="path/to/your/folder"`.

You can do a 'dry run' (nothing is actually analysed) with `flussli --dry` or `flussli --dry --folder="path/to/your/folder"`.


## For contributors

If you find bugs, or want new features, open an *issue* on the GitHub page.

If you want to directly contribute to the project, first clone the repo and install it as editable with developer dependencies:
```
git clone https://github.com/empaeconversion/flussli.git

cd flussli

pip install -e .[dev]
```
Then open this folder in an editor like VSCode.

You should always create a new branch before making edits, `main` is only for tested, working code that everyone can use.

E.g. make and switch to a new branch with
```
git checkout -b muendungsflussli
```

You have now branched off the `main` flussli into your own branch called e.g. `muendungsflussli`. Now you can do whatever you like and it won't affect the `main` branch.

When you have made and committed some changes that add a feature or fix a problem, and you want to put them into `main`, open a *pull request* on GitHub.
