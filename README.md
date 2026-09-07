# 🌊 PyFlowBatt

This Python package contains functions and a CLI for analysing data from the flow battery lab at Empa.


## Installation

Use Python >3.10, and install with pip:
```
pip install git+https://github.com/empaeconversion/pyflowbatt.git
```

## Usage

Use in a terminal (e.g. Command Prompt or PowerShell).

To analyse data and get a full summary and ro-crate, use `pyflowbatt --folder="path/to/your/folder"`.

To zip everything at the end (e.g. to upload to Zenodo), use `pyflowbatt --folder="path/to/your/folder" --zip`.

You can also navigate to a folder e.g.
```
cd path/to/your/folder
pyflowbatt
```
PyFlowBatt will automatically scan for files like GCPL, EIS, LSV, CV, as well as BattInfo Converter .xlsx files, process the data, convert to battery data format, produce summary graphs and excel reports, create a BattInfo JSON-LD metadata file, and a top level ro-crate metadata file.

A full analysis can take a while. You can do a 'dry run' to check what files *would* be analysed:
```
pyflowbatt --dry
```

```
pyflowbatt --dry --folder="path/to/your/folder" --zip
```
This is useful for checking everything is in order before running a long analysis.

You can override some settings by adding a `pyflowbatt.toml` file to a sample folder, parent folder, or your home folder. Use `pyflowbatt init` to create a template file and see what can be modified.

## Using in Python

You can also use the functions in Python, e.g.

```python
# Use individual functions
from pyflowbatt import gcpl, lsv, cv
from pyflowbatt.read import read_to_bdf

df = read_to_bdf("path/to/my/gcpl-file")
df, cycle_df = gcpl.analyse(df)

cv_df = read_to_bdf("path/to/my/cv-file")
cv_df, cv_cycle_df, capacitance_mF = cv.analyse(cv_df)

lsv_df = read_to_bdf("path/to/my/lsv-file")
lsv_result_dict = lsv.analyse(lsv_df)

# Or run the full analysis on folders
from pyflowbatt.analysis import analyse_sample, analyse_all_samples
analyse_sample("path/to/sample/folder")
analyse_all_samples("path/to/parent/folder/of/many/samples")
```

## For contributors

If you find bugs, or want new features, open an *issue* on the GitHub page.

If you want to directly contribute to the project, it is easiest to use the tool 'uv':
```
git clone https://github.com/empaeconversion/pyflowbatt.git

cd pyflowbatt

uv sync
```
Then open this folder in an editor like VSCode.

You should always create a new branch before making edits, `main` is only for tested, working code that everyone can use.

E.g. make and switch to a new branch with
```
git checkout -b new-feature
```

You have now branched off the `main` PyFlowBatt into your own branch called e.g. `new-feature`. Now you can do whatever you like and it won't affect the `main` branch.

When you have made and committed some changes that add a feature or fix a problem, and you want to put them into `main`, open a *pull request* on GitHub.
