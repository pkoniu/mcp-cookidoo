# Plan: Meal-plan calendar and shopping list tools

## Starting point

- The server (`server.py`) exposes 8 tools for search, recipe details and custom recipes. All
  Cookidoo calls go through `CookidooService` (`cookidoo_service.py`), which holds one
  logged-in `cookidoo_api.Cookidoo` client.
- The pinned library, `cookidoo-api` 0.19, **already implements both features**. Nothing has
  to be reverse-engineered. The work is service wrappers, MCP tools, tests and docs.

| Area | `Cookidoo` methods available in 0.19 |
| --- | --- |
| Calendar | `get_recipes_in_calendar_week(day, resolve_custom_recipes=)`, `add_recipes_to_calendar(day, ids)`, `remove_recipe_from_calendar(day, id)`, `add_custom_recipes_to_calendar(day, ids)`, `remove_custom_recipe_from_calendar(day, id)` |
| Shopping list: recipes | `get_shopping_list_recipes()`, `add_ingredient_items_for_recipes(ids)`, `remove_ingredient_items_for_recipes(ids)`, `add_ingredient_items_for_custom_recipes(ids)`, `remove_ingredient_items_for_custom_recipes(ids)` |
| Shopping list: ingredients | `get_ingredient_items()`, `edit_ingredient_items_ownership(items)` |
| Shopping list: own items | `get_additional_items()`, `add_additional_items(names)`, `edit_additional_items(items)`, `edit_additional_items_ownership(items)`, `remove_additional_items(ids)` |
| Shopping list: all | `clear_shopping_list()` |

The relevant types are `CookidooCalendarDay` (`id`, `title`, `recipes`, `customer_recipe_ids`),
`CookidooCalendarDayRecipe` (`id`, `name`, `total_time`, `url`, `is_custom`), and
`CookidooIngredientItem` / `CookidooAdditionalItem` (`id`, `name`, `is_owned`, plus
`description` and `preparation` on ingredients).

## Design decisions

1. **One tool for both recipe kinds.** The API has separate endpoints for catalogue recipes
   and custom recipes. The MCP tools take a list of IDs and route each one with a helper
   `_is_catalogue_id(id)`, which matches catalogue IDs like `r59322` (`^r\d+$`). Everything
   else is treated as a custom-recipe ID. The model then doesn't need to know which endpoint
   to call.
2. **Dates are ISO `YYYY-MM-DD` strings**, parsed with `date.fromisoformat`. An empty
   string means today. A bad value raises `ToolError`.
3. **Ticking items needs the current list.** The `edit_*_ownership` methods take full item
   objects, not IDs. The service fetches ingredient and additional items, matches the given
   IDs, sets `is_owned` on copies (`dataclasses.replace`) and sends each group to its
   endpoint. Unknown IDs raise an error.
4. **Tool annotations** follow the existing `READ_ONLY` / `WRITE` dicts. Removals and
   `clear_shopping_list` are marked `destructiveHint: True`.
5. **Output is plain text** with IDs inline, the same style as `search_recipes`, so the
   model can chain calls.
6. **Server `instructions`** get one more sentence: confirm with the user before clearing
   the shopping list or removing several planned meals.

## Steps

### 0. Dependency updates (separate commit, before the feature work)

Versions were checked against PyPI and GitHub on 2026-10-31. All of these are drop-in
updates, so do them first and run the existing tests to confirm a clean starting point.

| Dependency | Locked / pinned | Latest | Action |
| --- | --- | --- | --- |
| `cookidoo-api` | 0.19.0 | 0.19.0 | Already the latest. The `<0.20` pin stays. |
| `fastmcp` | 4.1.1 | 4.1.4 | Patch update within `<5`. Run `uv lock --upgrade`. |
| `pydantic` | 2.14.1 | 2.14.2 | Patch update. |
| `python-dotenv` | 1.2.4 | 1.2.4 | Already the latest. |
| `pytest` / `pytest-asyncio` / `ruff` (dev) | 9.1.1 / 1.4.0 / 0.17.1 | 9.2.0 / 1.4.1 / 0.17.3 | Minor and patch updates. Run `ruff format` again in case its output changes. |
| `actions/checkout` | v4 | v7 | Major version bump; the existing inputs are unchanged. |
| `astral-sh/setup-uv` | v6 | v10 | Major version bump. The workflow passes no inputs, so the default behaviour applies. |
| `docker/setup-qemu-action`, `setup-buildx-action`, `login-action` | v3 | v4 | Major version bump. |
| `docker/metadata-action` | v5 | v6 | Major version bump. |
| `docker/build-push-action` | v6 | v7 | Major version bump. |

Steps:

1. Run `uv lock --upgrade`, then `uv sync`.
2. Update the `uses:` tags in `.github/workflows/ci.yml` and `docker.yml`.
3. Run `uv run pytest` and `uv run ruff check . && uv run ruff format --check .`.
4. Push to the fork and confirm that CI passes.

**Status:** the Python updates are done. The GitHub Actions bumps are on the local branch
`ci/bump-actions` and haven't been pushed. Pushing changes to workflow files needs a token
with the `workflow` scope (`gh auth refresh -h github.com -s workflow`).

If any action's new major version fails with the existing inputs, keep that action on its
old tag. Don't rework the workflow for it.

The Docker base image (`uv:python3.12-bookworm-slim`) and `requires-python >= 3.12` stay as
they are. Moving to Python 3.13 or 3.14 isn't needed for this work.

### 1. Service layer: `cookidoo_service.py`

Add thin async wrappers that follow the existing `api = await self.client()` pattern:

- `get_calendar_week(day: date) -> list[CookidooCalendarDay]`: calls the library with
  `resolve_custom_recipes=True`, so custom recipes come back with names.
- `add_to_calendar(day, recipe_ids) -> CookidooCalendarDay`: splits the IDs and calls the
  catalogue and/or custom endpoint. Returns the day from the last call.
- `remove_from_calendar(day, recipe_id) -> CookidooCalendarDay`
- `get_shopping_list() -> ShoppingList`: a small dataclass holding `recipes`, `ingredients`
  and `additional`, fetched with `asyncio.gather`.
- `add_recipes_to_shopping_list(recipe_ids)` and `remove_recipes_from_shopping_list(recipe_ids)`:
  split by recipe kind.
- `add_shopping_items(names) -> list[CookidooAdditionalItem]`
- `rename_shopping_item(item_id, name)`
- `set_items_owned(item_ids, owned: bool)`: see design decision 3.
- `remove_shopping_items(item_ids)`: own items only. The API has no per-item delete for
  recipe ingredients, so a recipe's ingredients are removed by removing the recipe.
- `clear_shopping_list()`

### 2. MCP tools: `server.py`

**Calendar (meal plan)**

| Tool | Args | Notes |
| --- | --- | --- |
| `get_meal_plan` | `week_of: str = ""` | Lists every day of the week with its recipes, IDs and times. |
| `add_to_meal_plan` | `day: str`, `recipe_ids: list[str] \| str` | Catalogue and custom recipes. Reuses `_split_list` for the string form. |
| `remove_from_meal_plan` | `day: str`, `recipe_id: str` | Destructive. |

**Shopping list**

| Tool | Args | Notes |
| --- | --- | --- |
| `get_shopping_list` | — | Groups: ingredients still to buy, ingredients already owned, own items, plus the recipes they come from. Shows IDs. |
| `add_recipes_to_shopping_list` | `recipe_ids` | Adds the recipes' ingredients. |
| `remove_recipes_from_shopping_list` | `recipe_ids` | Destructive. |
| `add_shopping_items` | `items: list[str] \| str` | Free-text items ("kitchen roll", "2 lemons"). |
| `mark_shopping_items` | `item_ids`, `owned: bool = True` | Ticks or unticks ingredients and own items. |
| `rename_shopping_item` | `item_id`, `name` | Own items only. |
| `remove_shopping_items` | `item_ids` | Own items only. Destructive. |
| `clear_shopping_list` | — | Destructive. The tool description tells the model to confirm with the user first. |

**Convenience workflow**

- `shop_for_meal_plan(week_of: str = "")`: reads the week's planned recipes and adds all of
  them to the shopping list in one call. This covers the usual "plan the week, then shop"
  flow.
- Prompt `plan_my_week(days: int = 7, preferences: str = "")`: tells the model to search
  recipes, propose a plan, get approval, call `add_to_meal_plan` for each day, then offer
  `shop_for_meal_plan`. Add it next to `create_thermomix_recipe`, optionally with
  `prompt.md`-style EN/FR text files.

### 3. Tests: `tests/test_server.py`

- Add the new service methods to the `service` fixture's `AsyncMock` list, and the new
  names to `test_all_tools_registered`.
- Add one test per tool that checks the formatted output and the arguments passed to the
  service. Build the return values from the real `cookidoo_api` dataclasses.
- Add service-level tests that mock the `Cookidoo` client, covering:
  - routing mixed `r123` and custom IDs to both endpoints;
  - `set_items_owned` across ingredient and own items, and rejection of unknown IDs;
  - date parsing (empty string, valid date, invalid date).

### 4. Docs and release

- README: list the new features, add the new rows to "Available Tools", and add a short
  "Meal plan & shopping list" section with an example flow.
- CHANGELOG: add an entry. Bump `pyproject.toml` to `1.2.0`.
- Run `uv run pytest` and `uv run ruff check . && uv run ruff format .`.

### 5. Live check against a real account

Mocks can't verify these, so check them manually with the MCP Inspector
(`uv run fastmcp dev inspector fastmcp.json`):

- Which weekday Cookidoo treats as the start of the week, and whether `get_meal_plan`
  should list empty days.
- Whether custom-recipe ingredients show up in `get_ingredient_items` after
  `add_ingredient_items_for_custom_recipes`.
- Whether `add_additional_items` with a quantity in the text ("2 lemons") is kept as typed.
- The custom-recipe ID format, to confirm the `^r\d+$` routing rule.

## Out of scope (possible later work)

Collections (`get_managed_collections`, custom collections) and devices / subscription
info are also in the library. They could reuse the same pattern in a later PR.
