Add a local WSL cache guard that reclaims page cache only below the Windows
free-memory floor. Provide a three-minute root system timer and service.
Host sync installs the units and derives their floor from the fleet placement
policy for Windows-memory hosts.
