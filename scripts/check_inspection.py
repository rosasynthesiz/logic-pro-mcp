"""Exercise actual stdio MCP discovery and ensure every endpoint refuses execution."""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['LOGIC_MCP_INSPECT_ONLY'] = '1'
from mcp import ClientSession, StdioServerParameters, stdio_client
from logic_mcp import server, local

BLOCKED = ('AppKit', 'Quartz', 'ApplicationServices', 'logic_mcp.channels.midi',
           'logic_mcp.upload', 'logic_mcp.doctor', 'logic_mcp.policy', 'logic_mcp.target')
assert not any(n in sys.modules for n in BLOCKED)
for tool in local.tools():
    assert local.call(tool['name'], {})['refused'] is True
    assert local._impls[tool['name']]()['refused'] is True
assert not any(n in sys.modules for n in BLOCKED)


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


async def check_local():
    # Cover the CLI flag, environment selector, dedicated console entry point,
    # and python -m route. Every child checks its import state after shutdown.
    entries = [
        ("from logic_mcp.server import main; main(['--profile', 'local'])", {}),
        ("from logic_mcp.server import main; main([])", {'LOGIC_MCP_PROFILE': 'local'}),
        ("from logic_mcp.local import main; main()", {}),
        ("import runpy; runpy.run_module('logic_mcp.local', run_name='__main__')", {}),
    ]
    for entry, extra in entries:
        code = ("import sys\ntry:\n    " + entry + "\nfinally:\n    assert not any(n in sys.modules for n in "
                + repr(BLOCKED) + ")")
        params = StdioServerParameters(command=sys.executable, args=['-c', code], env=dict(os.environ) | extra)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert 'SCHEMA INSPECTION ONLY' in init.instructions
                tools = (await session.list_tools()).tools
                assert [t.name for t in tools] == [t['name'] for t in local.tools()]
                for tool in tools:
                    schema = tool.input_schema
                    args = {key: value(schema['properties'][key]) for key in schema.get('required', [])}
                    result = await session.call_tool(tool.name, args)
                    assert result.is_error and 'Inspection-only mode' in str(result), (entry, tool.name, result)
        # stdio_client surfaces abnormal child exits; parent direct calls above
        # independently assert the same import boundary.
    print('PASS: all 8 local tools refused through 4 entry routes; no native/backend imports')

asyncio.run(check())
asyncio.run(check_local())
