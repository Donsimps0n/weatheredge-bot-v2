#!/bin/bash
# Start both the Flask API server and the scheduler in parallel
python scheduler.py &
SCHEDULER_PID=$!
python api_server.py
# If API server exits, kill scheduler too
kill $SCHEDULER_PID 2>/dev/null
