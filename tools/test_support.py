"""Native test processes are always silent; never alter the user's preferences."""
import os

os.environ['RBO_TEST_SILENT'] = '1'
