#!/bin/bash

# Fix permissions for the /logs directory if it exists
if [ -d "/logs" ]
then
    echo "Fixing permissions for /logs directory..."
    chown ankerctl:ankerctl /logs || echo "Warning: Could not change ownership of /logs"
    chmod 755 /logs || echo "Warning: Could not change permissions of /logs"
fi

# Switch to the ankerctl user and execute the main command
exec su ankerctl -c "$*"
