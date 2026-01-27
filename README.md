# ines-flextool

Export ines model to FlexTool (and import FlexTool model to ines - not done yet).

Usage:

```
clone repository
cd to repository root
pip install .
cd to ines_flextool
python ines_to_flextool.py <sqlite:///ines_source_db.sqlite> <sqlite:///flextool_target_db.sqlite>
```

Example ines_source db can be obtained from ines-spec repository and flextool_target_db is provided for convenience in the repository root (But should be obtained from IRENA FlexTool repository once it has been upgraded to Spine Toolbox v0.8).
