## HOST-RUN-CAPS

- Source Python overflow run caps and memory floors from the worker-core hosts
  registry, resolving IDs/aliases and local identity rather than trusting copied
  overflow configuration limits. Missing or invalid caps fail closed remotely
  and log conservative local defaults (4 runs / 3000 MB).
- Show the three D-5116 worker profiles (10/8/4096, 12/8/8192, 3/2/2048) in
  the generic registry example. Existing V6 worker bounds already accept these;
  restricted-host limits are unchanged.
