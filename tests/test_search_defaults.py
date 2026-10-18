"""Retrieval defaults through a mounted proxy; fake upstream, no network or vault."""

import unittest

from fastmcp import Client, FastMCP


class SearchDefaultsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from g_brain import surface

        self.surface = surface
        self.calls = []
        upstream = FastMCP("basic-memory")

        @upstream.tool()
        def search_notes(
            query: str | None = None,
            project: str | None = None,
            project_id: str | None = None,
            search_all_projects: bool = False,
            page: int = 1,
            page_size: int = 10,
            search_type: str | None = None,
            output_format: str = "text",
            note_types: list[str] | None = None,
            entity_types: list[str] | None = None,
            categories: list[str] | None = None,
            after_date: str | None = None,
            metadata_filters: dict | None = None,
            tags: list[str] | None = None,
            status: str | None = None,
            min_similarity: float | None = None,
        ) -> dict:
            arguments = locals()
            arguments.pop("self")
            self.calls.append(arguments)
            return arguments

        @upstream.tool()
        def build_context(url: str, timeframe: str | None = "7d") -> dict:
            arguments = {"url": url, "timeframe": timeframe}
            self.calls.append(arguments)
            return arguments

        @upstream.tool()
        def read_note(identifier: str) -> str:
            return identifier

        for name in surface.BLOCKED_TOOLS:
            upstream.tool(lambda: "written", name=name)

        server = FastMCP("brain")
        server.mount(surface.proxy_basic_memory(upstream))
        surface.lock_write_surface(server)
        self.client = await self.enterAsyncContext(Client(server))

    async def test_unscoped_search_uses_kept_kinds_and_compact_five_results(self):
        await self.client.call_tool("search_notes", {"query": "architecture"})
        self.assertEqual(self.calls[-1]["metadata_filters"], {
            "kind": {"$in": ["design", "idea", "fact", "hazard"]},
        })
        self.assertEqual(self.calls[-1]["page_size"], 5)
        self.assertEqual(self.calls[-1]["output_format"], "text")

    async def test_explicit_filters_take_precedence(self):
        for name, value in (
            ("metadata_filters", {"kind": "todo"}),
            ("note_types", ["task"]),
            ("entity_types", ["observation"]),
            ("categories", ["decision"]),
            ("tags", ["work"]),
            ("status", "open"),
        ):
            with self.subTest(name=name):
                await self.client.call_tool("search_notes", {"query": "architecture", name: value})
                self.assertEqual(self.calls[-1][name], value)
                if name != "metadata_filters":
                    self.assertIsNone(self.calls[-1]["metadata_filters"])

    async def test_empty_metadata_filters_search_everything(self):
        await self.client.call_tool("search_notes", {
            "query": "architecture", "metadata_filters": {},
        })
        self.assertEqual(self.calls[-1]["metadata_filters"], {})

    async def test_null_filters_keep_the_default_scope(self):
        await self.client.call_tool("search_notes", {
            "query": "architecture", "metadata_filters": None, "tags": None,
        })
        self.assertIn("kind", self.calls[-1]["metadata_filters"])

    async def test_project_and_path_overrides_take_precedence(self):
        for arguments in (
            {"project": "other"},
            {"project": "brain"},
            {"project_id": "00000000-0000-0000-0000-000000000001"},
            {"search_all_projects": True},
            {"query": "todos/*", "search_type": "permalink"},
            {"query": "memory://brain/todos/example"},
            {"query": "todos/example"},
            {"query": "example", "search_type": "permalink"},
            {"query": "tag:work"},
            {"query": "architecture TAG:work"},
        ):
            with self.subTest(arguments=arguments):
                sent = {"query": "architecture", **arguments}
                await self.client.call_tool("search_notes", sent)
                self.assertIsNone(self.calls[-1]["metadata_filters"])
                for name, value in sent.items():
                    self.assertEqual(self.calls[-1][name], value)

    async def test_explicit_pagination_format_and_search_options_survive(self):
        arguments = {"query": "architecture", "page": 2, "page_size": 12,
                     "output_format": "json", "search_type": "hybrid",
                     "after_date": "2026-01-01", "min_similarity": 0.7}
        await self.client.call_tool("search_notes", arguments)
        for name, value in arguments.items():
            self.assertEqual(self.calls[-1][name], value)
        self.assertIn("kind", self.calls[-1]["metadata_filters"])

    async def test_filter_only_search_preserves_the_callers_scope(self):
        await self.client.call_tool("search_notes", {"metadata_filters": {"kind": "todo"}})
        self.assertIsNone(self.calls[-1]["query"])
        self.assertEqual(self.calls[-1]["metadata_filters"], {"kind": "todo"})

    async def test_build_context_has_no_default_cutoff(self):
        await self.client.call_tool("build_context", {"url": "memory://designs/example"})
        self.assertIsNone(self.calls[-1]["timeframe"])

    async def test_build_context_preserves_an_explicit_cutoff(self):
        await self.client.call_tool("build_context", {
            "url": "memory://designs/example", "timeframe": "30d",
        })
        self.assertEqual(self.calls[-1]["timeframe"], "30d")

    async def test_discovery_describes_the_defaults_and_opt_out(self):
        tools = {tool.name: tool for tool in await self.client.list_tools()}
        search = tools["search_notes"]
        properties = search.input_schema["properties"]
        self.assertEqual(properties["page_size"]["default"], 5)
        self.assertEqual(properties["output_format"]["default"], "text")
        for layer in ("designs/", "ideas/", "about/", "hazards/"):
            self.assertIn(layer, search.description)
        self.assertIn("metadata_filters={}", search.description)
        context = tools["build_context"]
        self.assertIsNone(context.input_schema["properties"]["timeframe"]["default"])
        self.assertIn("no recency cutoff", context.description)

    async def test_other_reads_work_and_writes_stay_hidden_and_uncallable(self):
        names = {tool.name for tool in await self.client.list_tools()}
        self.assertTrue({"search_notes", "build_context", "read_note"} <= names)
        self.assertFalse(names & self.surface.BLOCKED_TOOLS)
        result = await self.client.call_tool("read_note", {"identifier": "todos/example"})
        self.assertEqual(result.content[0].text, "todos/example")
        for name in self.surface.BLOCKED_TOOLS:
            with self.subTest(name=name), self.assertRaises(Exception):
                await self.client.call_tool(name, {})


    async def test_slash_terms_stay_scoped_and_vault_paths_do_not(self):
        scoped = {"kind": {"$in": ["design", "idea", "fact", "hazard"]}}
        for query in ("CI/CD", "input/output", "TCP/IP"):
            with self.subTest(query=query):
                await self.client.call_tool("search_notes", {"query": query})
                self.assertEqual(self.calls[-1]["metadata_filters"], scoped)
        for query in ("todos/pay-rent", "findings/x.md", "memory://brain/designs/x"):
            with self.subTest(query=query):
                await self.client.call_tool("search_notes", {"query": query})
                self.assertIsNone(self.calls[-1]["metadata_filters"])
