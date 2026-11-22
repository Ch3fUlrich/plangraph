Keeper stores one outcome per terminal spawn with an optional card foreign key,
a validated route envelope and witnessed serving metadata. The attempts command
reads these rows by time window.
Fixer and native writer runs enter the same capture path. Rows preserve supplied
item keys for spawns whose keeper card is absent.
