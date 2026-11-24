#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容入口：批量主键查询。实现见 key_query.py。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from key_query import *  # noqa: F401,F403
from key_query import (  # noqa: F401
    QUERY_VERSION,
    QueryRejection,
    build_key_report,
    main,
    query_keys_in_memory,
    run_key_query,
)

if __name__ == "__main__":
    sys.exit(main())
