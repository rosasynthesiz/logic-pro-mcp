"""Exercise actual stdio MCP discovery and ensure every endpoint refuses execution."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['LOGIC_MCP_INSPECT_ONLY'] = '1'
from mcp import ClientSession, StdioServerParameters, stdio_client
from logic_mcp import server

assert not any(n in sys.modules for n in ('AppKit', 'Quartz', 'ApplicationServices', 'logic_mcp.channels.midi', 'logic_mcp.upload'))


def value(schema):
    if 'enum' in schema:
        return schema['enum'][0]
    if 'anyOf' in schema:
        return value(schema['anyOf'][0])
    return {'string': 'inspection-test', 'integer': 1, 'number': 1.0, 'boolean': False, 'array': [], 'object': {}}[schema['type']]


async def check():
    params = StdioServerParameters(command=sys.executable, args=['-c', 'from logic_mcp.server import main; main()'], env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert 'SCHEMA INSPECTION ONLY' in init.instructions
            tools = (await session.list_tools()).tools
            resources = (await session.list_resources()).resources
            assert len(tools) == 12 and len(resources) == 7
            for tool in tools:
                schema = tool.input_schema
                args = {key: value(schema['properties'][key]) for key in schema.get('required', [])}
                result = await session.call_tool(tool.name, args)
                assert result.is_error, (tool.name, result)
                assert 'Inspection-only mode' in str(result), (tool.name, result)
            for resource in resources:
                try:
                    await session.read_resource(resource.uri)
                except Exception as exc:
                    assert 'Inspection-only mode' in str(exc), exc
                else:
                    raise AssertionError(f'Resource unexpectedly succeeded: {resource.uri}')
    print('PASS: stdio discovery, 12 blocked tools, 7 blocked resources, no native/backend imports')


asyncio.run(check())
