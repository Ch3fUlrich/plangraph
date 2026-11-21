The Python MCP source guard now refuses probes after a source change. Session
launch and relaunch honor a `fleet-launch.pause` file in the spawner state
directory and surface its reason before starting or stopping a session.
