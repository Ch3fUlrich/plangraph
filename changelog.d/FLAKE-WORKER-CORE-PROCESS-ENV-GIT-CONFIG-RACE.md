The pilot-credentials sandbox test runs in a one-test child process, so the Git controls it sets as a parent environment never reach parallel Git fixtures in the worker-core test process.
