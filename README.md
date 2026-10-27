# 🌊 PyFlowBatt

This Python package contains functions and a CLI for analysing data from the flow battery lab at Empa.


## Installation

Use Python >=3.10, and install with pip:
```
pip install pyflowbatt
```

## Usage

Use in a terminal (e.g. Command Prompt or PowerShell).

To analyse the data in a folder, use
```
pyflowbatt --folder "path/to/your/folder"
```
Or go to a folder and just run `pyflowbatt`:
```
cd path/to/your/folder
pyflowbatt
```

PyFlowBatt processes the data, converts it to battery data format, makes summary graphs and Excel reports, and creates a BattINFO JSON-LD metadata file.

### How to lay out data

Make one folder per sample (a "sample folder") and put raw data in it (not in subfolders). PyFlowBatt recognises each measurement from its file name, so the names need to contain `_OCV_`, `_PEIS_`, `_LSV_`, `_GCPL_`, `_CVpre` or `_CVpost`. If your files are named differently, you can change this in a `pyflowbatt.toml` file (see below).

You can run PyFlowBatt on one sample folder, or on a folder with many sample folders inside it:

```
my-experiment/               <- run pyflowbatt here
├── 261028_abcd_cell1/        <- a sample folder
│   ├── ..._01_OCV_CE4.mpr
│   ├── ..._02_PEIS_CE4.mpr
│   ├── ..._04_GCPL_CE4.mpr
│   ├── battinfo.xlsx        <- optional, from BattINFO Converter
│   └── results/             <- made by PyFlowBatt
├── 261028_abcd_cell2/       <- another sample folder
│   └── ...
└── combined_summary.xlsx    <- made by PyFlowBatt
```

Name each sample folder starting with the date and your initials, e.g. `250115_abcd_...`. PyFlowBatt uses the folder name as the sample name in the summaries.

Sample folders can be grouped into subfolders, but don't put a sample folder inside another sample folder.

### Zipping for Zenodo

If you use `--zip`, PyFlowBatt also makes a zip of everything, with a RO-Crate metadata file describing the files inside:
```
pyflowbatt --folder "path/to/your/folder" --zip
```

### Several folders at once

You can analyse several folders in one go. Each folder is analysed as if run on its own (including the zip, if used).
```
pyflowbatt --folder "path/to/X" "path/to/Y" "path/to/Z"
```
You can also combine all those summaries from multiple folders into one Excel file: add `--combined-summary` with an `.xlsx` file, or a folder to write `combined_summary.xlsx` into (`.` for the current folder).
```
pyflowbatt --folder "path/to/X" "path/to/Y" "path/to/Z" --combined-summary "path/to/all_samples.xlsx"
```
Its "Summary" sheet lists every sample from all the folders, and its "By folder" sheet puts each folder's average and standard deviation side by side.

### Checking before a long run

A full analysis can take a while. You can do a 'dry run' to check what files *would* be analysed, e.g.:
```
pyflowbatt --dry
```
```
pyflowbatt --dry --folder "path/to/your/folder" --zip
```
This is useful for checking everything is in order before running a long analysis.

### Settings

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
