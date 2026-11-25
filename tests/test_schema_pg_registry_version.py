"""One reader of the keeper migration registry for the batch land gate and the Rust train gate."""
import pytest

from plangraph.keeper import schema_pg
from plangraph.keeper.schema_pg import registry_version

REAL = "MIGRATIONS = (Migration(1, 'a', X), Migration(2, 'b', Y))\n"


def test_the_real_registry_reads_as_latest():
    source = open(schema_pg.__file__, encoding="utf-8").read()
    assert registry_version(source) == schema_pg.LATEST


@pytest.mark.parametrize("source", [
    '"""Shape:\nMIGRATIONS = (Migration(1, a),)\n"""\n' + REAL,
    "# MIGRATIONS = (Migration(1, a),)\n" + REAL,
    "MIGRATIONS=(Migration(1, 'a', X),\n  Migration(2, 'b', Y))\n",
    "X = 1; " + REAL,
])
def test_only_the_top_level_registry_counts(source):
    assert registry_version(source) == 2


@pytest.mark.parametrize("source", [
    "MIGRATIONS = (Migration(1, a),)\n" + REAL,
    "MIGRATIONS = (Migration(1, a),)\nX = 1; MIGRATIONS=(Migration(1, a), Migration(2, b))\n",
    "MIGRATIONS = (Migration(1, a),)\nX = (1,\n  2); MIGRATIONS = (Migration(1, a), Migration(2, b))\n",
    "MIGRATIONS = (Migration(1, a), Migration(3, c))\n",
    "MIGRATIONS = (Migration(1, a), [Migration(2, b)])\n",
    "MIGRATIONS = tuple(load())\n",
    "x = 1\n",
    "MIGRATIONS = (Migration(1, a),\n",
    # writes the static read cannot account for: the keeper would load another registry
    REAL + "MIGRATIONS: tuple = (Migration(1, a), Migration(2, b), Migration(3, c))\n",
    REAL + "MIGRATIONS += (Migration(3, c),)\n",
    REAL + "from other import MIGRATIONS\n",
    REAL + "import other as MIGRATIONS\n",
    REAL + "def MIGRATIONS():\n    pass\n",
    REAL + "def f():\n    global MIGRATIONS\n    MIGRATIONS = ()\n",
    REAL + "for MIGRATIONS in []:\n    pass\n",
    REAL + "if X:\n    MIGRATIONS = (Migration(1, a),)\n",
    # a conditional write cannot be accounted for statically, even a dead one
    "if False:\n    MIGRATIONS = (Migration(1, a),)\n" + REAL,
    "if False: MIGRATIONS = (Migration(1, a),)\n" + REAL,
])
def test_a_missing_repeated_or_malformed_registry_is_refused(source):
    with pytest.raises(ValueError):
        registry_version(source)
