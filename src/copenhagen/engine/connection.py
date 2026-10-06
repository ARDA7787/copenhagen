"""Shared Temporal transport settings; safe to import in domain workers."""

import os

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter


async def connect_temporal(address: str, namespace: str, *, lazy: bool = False) -> Client:
    return await Client.connect(
        address,
        namespace=namespace,
        data_converter=pydantic_data_converter,
        tls=os.environ.get("TEMPORAL_TLS", "0") == "1",
        api_key=os.environ.get("TEMPORAL_API_KEY"),
        lazy=lazy,
    )
