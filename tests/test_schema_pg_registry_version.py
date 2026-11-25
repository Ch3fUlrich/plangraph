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


def _runtime_latest(source):
    """What the keeper would load: run the registry source with a stand-in ``Migration`` and read ``LATEST``."""
    from collections import namedtuple

    namespace = {"Migration": namedtuple("Migration", "version name ddl", defaults=(None, None)), "X": 0, "Y": 0, "Z": 0}
    exec(source + "\nLATEST = MIGRATIONS[-1].version\n", namespace)  # noqa: S102 - fixture text only
    return namespace["LATEST"]


@pytest.mark.parametrize("source", [
    REAL,
    "MIGRATIONS = [Migration(1, 'a', X)]\nMIGRATIONS.append(Migration(2, 'b', Y))\n",
    "MIGRATIONS = [Migration(1, 'a', X), Migration(2, 'b', Y)]\n",
    REAL + "MIGRATIONS += (Migration(3, 'c', Z),)\n",
    REAL + "MIGRATIONS = MIGRATIONS + (Migration(3, 'c', Z),)\n",
    "MIGRATIONS = [Migration(1, 'a', X)]\nMIGRATIONS.extend([Migration(2, 'b', Y)])\n",
    "MIGRATIONS = [Migration(1, 'a', X)]\nMIGRATIONS[:] = [Migration(1, 'a', X), Migration(2, 'b', Y)]\n",
    "if False: MIGRATIONS = (Migration(1, 'a', X),)\n" + REAL,
])
def test_the_reader_never_reports_a_version_other_than_the_one_the_keeper_loads(source):
    """Every shape either raises or agrees with the runtime ``LATEST``: no write hides a schema rise."""
    try:
        read = registry_version(source)
    except ValueError:
        return
    assert read == _runtime_latest(source)


def test_a_list_registry_is_refused_because_it_can_be_appended_to():
    with pytest.raises(ValueError, match="tuple"):
        registry_version("MIGRATIONS = [Migration(1, 'a', X)]\nMIGRATIONS.append(Migration(2, 'b', Y))\n")
