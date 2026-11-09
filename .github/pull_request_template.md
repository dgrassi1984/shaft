## Problem

Describe the problem and when it occurs.

## Change

Describe the resulting behavior.

## How you tested

Record the results of these commands and any relevant manual checks. `gate budget` tests committed code, so commit before running it.

```sh
python3 -m unittest discover -s supervisor/tests -t .
bin/shaft gate budget
```

- [ ] The diff contains no `.env`, instance data, credentials, or real mail.
