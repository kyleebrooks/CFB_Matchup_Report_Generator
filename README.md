# CFB Matchup Report Generator

## Local Rotowire database

The application writes Rotowire articles to a local SQLite database. By default the file `rotowire.db` is created in the project root.

To place the database elsewhere, set the `ROTOWIRE_DB_PATH` environment variable to the full path where the `.db` file should live, e.g.

```bash
export ROTOWIRE_DB_PATH=/var/cfb/rotowire.db
```

Make sure the process has read and write access to the directory.
