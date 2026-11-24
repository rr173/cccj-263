#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容入口：batch_lookup。实现见 key_query.py。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from key_query import *  # noqa: F401,F403
from key_query import main, query_keys_in_memory, run_key_query  # noqa: F401

if __name__ == "__main__":
    sys.exit(main())
