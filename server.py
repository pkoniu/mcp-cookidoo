"""
Cookidoo MCP Server

Main server file containing MCP tool definitions for interacting with Cookidoo.
"""

import json
import re
import tomllib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from cookidoo_api import CookidooAdditionalItem, CookidooIngredientItem
from cookidoo_api.types import CookidooCalendarDay
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import ValidationError

from cookidoo_service import CookidooService, load_cookidoo_settings
from guided import from_raw
from schemas import CustomRecipe, Step

PROMPTS_DIR = Path(__file__).parent
VERSION = tomllib.loads((PROMPTS_DIR / "pyproject.toml").read_text())["project"]["version"]
READ_ONLY = {"readOnlyHint": True, "openWorldHint": True}
WRITE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}
DESTRUCTIVE = {**WRITE, "destructiveHint": True}

# Module-level authenticated session, created lazily on first use
_cookidoo_service: CookidooService | None = None


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[None]:
    try:
        yield
    finally:
        if _cookidoo_service is not None:
            await _cookidoo_service.close()


mcp = FastMCP(
    "cookidoo-mcp-server",
    version=VERSION,
    instructions=(
        "Tools for the Thermomix Cookidoo platform: search recipes, read them, create "
        "or edit the user's own custom recipes, plan meals on the Cookidoo calendar and "
        "manage the shopping list. Always show a recipe to the user and get explicit "
        "approval before creating or updating it, and confirm before clearing the "
        "shopping list or removing several planned meals."
    ),
    lifespan=lifespan,
)


def _get_service() -> CookidooService:
    global _cookidoo_service
    if _cookidoo_service is None:
        try:
            _cookidoo_service = CookidooService(load_cookidoo_settings())
        except ValueError as e:
            raise ToolError(f"Configuration error: {e}") from e
    return _cookidoo_service


def _steps(value: list[str | Step] | str) -> list[str | Step]:
    """Accept a list of plain or structured steps, or a string with one step per line."""
    if isinstance(value, str):
        return _lines(value, strip_numbering=True)
    steps: list[str | Step] = []
    for step in value:
        steps += [step] if isinstance(step, Step) else _lines([step], strip_numbering=True)
    return steps


def _lines(value: list[str] | str, strip_numbering: bool = False) -> list[str]:
    """Accept a list, or a string with one item per line (commas are kept)."""
    items = value.split("\n") if isinstance(value, str) else value
    if strip_numbering:
        items = [re.sub(r"^\s*(\d+[.)-]|[-•*])\s*", "", item) for item in items]
    return [item.strip() for item in items if item.strip()]


def _split_list(text: str) -> list[str]:
    """Split on newlines if present, otherwise on commas."""
    parts = text.split("\n") if "\n" in text else text.split(",")
    return [part.strip() for part in parts if part.strip()]


def _parse_recipe_json(recipe_json: str) -> CustomRecipe:
    try:
        return CustomRecipe.model_validate_json(recipe_json)
    except ValidationError as e:
        raise ToolError(f"Invalid recipe data: {e}") from e


def _minutes(seconds: int | None) -> str:
    return f"{round(seconds / 60)} min" if seconds else "?"


def _strip_markup(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _ids(value: list[str] | str) -> list[str]:
    """Accept a list of IDs, or a string of IDs separated by commas or newlines."""
    ids = _split_list(value) if isinstance(value, str) else [v.strip() for v in value]
    ids = [i for i in ids if i]
    if not ids:
        raise ToolError("Provide at least one ID.")
    return ids


def _parse_day(value: str) -> date:
    """An ISO date (YYYY-MM-DD); empty means today."""
    if not value.strip():
        return date.today()
    try:
        return date.fromisoformat(value.strip())
    except ValueError as e:
        raise ToolError(f"Invalid date '{value}': use the YYYY-MM-DD format.") from e


def _format_day(day: CookidooCalendarDay) -> list[str]:
    lines = [f"{day.title} ({day.id}):" if day.title != day.id else f"{day.id}:"]
    for recipe in day.recipes:
        kind = ", custom" if recipe.is_custom else ""
        lines.append(f"  - {recipe.name} ({_minutes(recipe.total_time)}{kind}) | ID: {recipe.id}")
    loaded = {recipe.id for recipe in day.recipes}
    for recipe_id in day.customer_recipe_ids:
        if recipe_id not in loaded:
            lines.append(f"  - Custom recipe that could not be loaded | ID: {recipe_id}")
    if len(lines) == 1:
        lines.append("  (nothing planned)")
    return lines


def _week_recipe_ids(days: list[CookidooCalendarDay]) -> list[str]:
    """Every recipe planned in the week, once, in calendar order."""
    ids = [r.id for day in days for r in day.recipes]
    ids += [i for day in days for i in day.customer_recipe_ids]
    return list(dict.fromkeys(ids))


def _item_line(item: CookidooIngredientItem | CookidooAdditionalItem) -> str:
    text = " ".join(p for p in (getattr(item, "description", ""), item.name) if p)
    if preparation := getattr(item, "preparation", None):
        text += f", {preparation}"
    return f"  - {text} | ID: {item.id}"


@mcp.tool(annotations=READ_ONLY)
async def connect_to_cookidoo() -> str:
    """
    Authenticate with Cookidoo using the credentials from the environment / .env file.

    Optional: every other tool connects automatically on first use. Call this to
    check the configuration or to force a fresh login.
    """
    service = _get_service()
    try:
        await service.login()
    except Exception as e:
        raise ToolError(f"Connection failed: {e}") from e
    return f"Connected to {service.localization_url} as {service.settings.email}"


@mcp.tool(annotations=READ_ONLY)
async def search_recipes(
    query: str = "",
    ingredients: str = "",
    max_total_time: int | None = None,
    max_results: int = 10,
) -> str:
    """
    Search the Cookidoo recipe catalogue.

    Use this to find inspiration or recipes based on what is in the fridge, then pass
    the returned IDs to get_recipe_details.

    Args:
        query: Free-text query (e.g. "risotto", "quick soup")
        ingredients: Ingredients that must be used, comma- or newline-separated
        max_total_time: Maximum total time in minutes
        max_results: Maximum number of recipes to return (default: 10)
    """
    if not (query or ingredients):
        raise ToolError("Provide a query and/or a list of ingredients.")
    try:
        result = await _get_service().search_recipes(
            query=query,
            ingredients=_split_list(ingredients),
            max_total_time=max_total_time,
            max_results=max_results,
        )
    except Exception as e:
        raise ToolError(f"Search failed: {e}") from e

    if not result.recipes:
        return "No recipes found. Try fewer or more common ingredients."
    lines = [f"Found {len(result.recipes)} recipes:", ""]
    for i, hit in enumerate(result.recipes, 1):
        lines.append(f"{i}. {hit.name}\n   ID: {hit.id} | {hit.url}")
    return "\n".join(lines)


@mcp.tool(annotations=READ_ONLY)
async def get_recipe_details(recipe_id: str) -> str:
    """
    Get the full details of a Cookidoo catalogue recipe: ingredients, steps, times.

    Args:
        recipe_id: The Cookidoo recipe ID (e.g. "r59322", "r907015")
    """
    try:
        recipe = await _get_service().get_recipe_details(recipe_id)
    except Exception as e:
        raise ToolError(f"Failed to get recipe details: {e}") from e

    lines = [
        f"Name: {recipe.name}",
        f"ID: {recipe.id}",
        f"Servings: {recipe.serving_size}",
        f"Active time: {_minutes(recipe.active_time)}",
        f"Total time: {_minutes(recipe.total_time)}",
        f"Difficulty: {recipe.difficulty}",
    ]
    if recipe.utensils:
        lines.append(f"Utensils: {', '.join(recipe.utensils)}")

    lines += ["", "Ingredients:"]
    for ingredient in recipe.ingredients:
        line = "  - " + " ".join(p for p in (ingredient.description, ingredient.name) if p)
        if ingredient.preparation:
            line += f", {ingredient.preparation}"
        lines.append(line)

    step_number = 0
    for group in recipe.step_groups:
        lines += ["", f"{group.title}:" if group.title else "Steps:"]
        for step in group.recipe_steps:
            step_number += 1
            lines.append(f"{step_number}. {_strip_markup(step.formatted_text)}")

    if recipe.notes:
        lines += ["", "Notes:", *(f"  - {_strip_markup(note)}" for note in recipe.notes)]
    lines += ["", f"URL: {recipe.url}"]
    return "\n".join(lines)


@mcp.tool(annotations=READ_ONLY)
async def list_custom_recipes() -> str:
    """List the custom recipes created on the user's Cookidoo account."""
    try:
        recipes = await _get_service().list_custom_recipes()
    except Exception as e:
        raise ToolError(f"Failed to list custom recipes: {e}") from e

    if not recipes:
        return "No custom recipes on this account yet."
    return "\n".join(
        f"- {r.name} ({r.serving_size} servings, {_minutes(r.total_time)})\n  ID: {r.id} | {r.url}"
        for r in recipes
    )


@mcp.tool(annotations=READ_ONLY)
async def get_custom_recipe(recipe_id: str) -> str:
    """
    Get one of the user's custom recipes as JSON, in the format expected by
    update_custom_recipe. Use it as the starting point before editing. Steps with
    machine settings or ingredient links come back as structured steps.

    Args:
        recipe_id: The custom recipe ID returned by list_custom_recipes
    """
    try:
        r, instructions = await _get_service().get_custom_recipe(recipe_id)
    except Exception as e:
        raise ToolError(f"Failed to get custom recipe: {e}") from e

    recipe = {
        "name": r.name,
        "ingredients": r.ingredients,
        "steps": [s if isinstance(s, str) else s.model_dump() for s in map(from_raw, instructions)],
        "servings": r.serving_size,
        "prep_time": max(1, round(r.active_time / 60)),
        "total_time": max(1, round(r.total_time / 60)),
        "hints": r.hints or None,
    }
    return f"URL: {r.url}\n\n{json.dumps(recipe, indent=2, ensure_ascii=False)}"


@mcp.tool(annotations=READ_ONLY)
async def generate_recipe_structure(
    name: str,
    ingredients: list[str] | str,
    steps: list[str | Step] | str,
    servings: int = 4,
    prep_time: int = 30,
    total_time: int = 60,
    hints: list[str] | str = "",
) -> str:
    """
    Generate and validate a recipe structure ready for upload to Cookidoo.

    Returns a JSON structure to show to the user and then pass to
    upload_custom_recipe or update_custom_recipe.

    Args:
        name: Recipe name (required)
        ingredients: Ingredients with quantities, as a list (or one per line)
        steps: Cooking steps, as a list (or one per line). A step without machine settings
            or ingredient links is plain text. Otherwise it is an object whose text holds one
            {settings} placeholder per entry of settings; the server writes each setting into
            the text and presets it on the Thermomix. A setting's "mode" picks its fields
            (allowed values are in the schema): "manual" (default: time, temperature, speed,
            reverse), "dough", "browning", "steaming", "turbo", "rice_cooker", "warm_up",
            "blend". "ingredients" links words of the text to an ingredient line. Example:
            {"text": "Add the onion and sauté {settings}.", "settings": [{"time": 180,
            "temperature": "120", "speed": "1"}], "ingredients": [{"text": "the onion",
            "ingredient": "1 onion"}]}
        servings: Number of servings (default: 4, range: 1-20)
        prep_time: Active preparation time in minutes (default: 30)
        total_time: Total time in minutes, at least prep_time (default: 60)
        hints: Optional cooking tips, as a list (or one per line)
    """
    try:
        recipe = CustomRecipe(
            name=name,
            ingredients=_lines(ingredients),
            steps=_steps(steps),
            servings=servings,
            prep_time=prep_time,
            total_time=total_time,
            hints=_lines(hints) or None,
        )
    except ValidationError as e:
        raise ToolError(f"Validation failed: {e}") from e
    return recipe.model_dump_json(indent=2)


@mcp.tool(annotations=WRITE)
async def upload_custom_recipe(recipe_json: str) -> str:
    """
    Create a brand new custom recipe on the user's Cookidoo account.

    Only call this after the user explicitly approved the recipe.

    Args:
        recipe_json: The validated recipe JSON from generate_recipe_structure
    """
    recipe = _parse_recipe_json(recipe_json)
    try:
        created = await _get_service().create_custom_recipe(recipe)
    except Exception as e:
        raise ToolError(f"Upload failed: {e}") from e
    return f"Recipe '{created.name}' created.\nRecipe ID: {created.id}\nURL: {created.url}"


@mcp.tool(annotations={**WRITE, "destructiveHint": True, "idempotentHint": True})
async def update_custom_recipe(recipe_id: str, recipe_json: str) -> str:
    """
    Replace the content of an existing custom recipe on Cookidoo.

    Get the current version with get_custom_recipe, edit it, show the result to the
    user and only call this after their explicit approval.

    Args:
        recipe_id: The custom recipe ID returned by list_custom_recipes
        recipe_json: The full, validated recipe JSON (same format as upload_custom_recipe)
    """
    recipe = _parse_recipe_json(recipe_json)
    try:
        updated = await _get_service().update_custom_recipe(recipe_id, recipe)
    except Exception as e:
        raise ToolError(f"Update failed: {e}") from e
    return f"Recipe '{updated.name}' updated.\nRecipe ID: {updated.id}\nURL: {updated.url}"


@mcp.tool(annotations=READ_ONLY)
async def get_meal_plan(week_of: str = "") -> str:
    """
    Get the Cookidoo meal plan (calendar) of a week: the recipes planned on each day.

    Args:
        week_of: Any date in the week, as YYYY-MM-DD (default: today)
    """
    day = _parse_day(week_of)
    try:
        days = await _get_service().get_calendar_week(day)
    except Exception as e:
        raise ToolError(f"Failed to get the meal plan: {e}") from e

    if not days:
        return f"Nothing planned in the week of {day.isoformat()}."
    lines = [f"Meal plan for the week of {day.isoformat()}:"]
    for calendar_day in days:
        lines += ["", *_format_day(calendar_day)]
    return "\n".join(lines)


@mcp.tool(annotations=WRITE)
async def add_to_meal_plan(day: str, recipe_ids: list[str] | str) -> str:
    """
    Plan recipes on a day of the Cookidoo calendar.

    Works with catalogue recipes (IDs like "r59322", from search_recipes) and the
    user's custom recipes (IDs from list_custom_recipes).

    Args:
        day: The date, as YYYY-MM-DD
        recipe_ids: Recipe IDs, as a list or comma-separated
    """
    when = _parse_day(day)
    ids = _ids(recipe_ids)
    try:
        calendar_day = await _get_service().add_to_calendar(when, ids)
    except Exception as e:
        raise ToolError(f"Failed to add to the meal plan: {e}") from e
    return "Meal plan updated.\n\n" + "\n".join(_format_day(calendar_day))


@mcp.tool(annotations=DESTRUCTIVE)
async def remove_from_meal_plan(day: str, recipe_id: str) -> str:
    """
    Remove a planned recipe from a day of the Cookidoo calendar.

    Args:
        day: The date the recipe is planned on, as YYYY-MM-DD
        recipe_id: The recipe ID, as shown by get_meal_plan
    """
    when = _parse_day(day)
    try:
        calendar_day = await _get_service().remove_from_calendar(when, recipe_id.strip())
    except Exception as e:
        raise ToolError(f"Failed to remove from the meal plan: {e}") from e
    return "Meal plan updated.\n\n" + "\n".join(_format_day(calendar_day))


@mcp.tool(annotations=READ_ONLY)
async def get_shopping_list() -> str:
    """
    Get the Cookidoo shopping list: the recipes on it, their ingredients and the user's
    own items, split into what is still to buy and what is already ticked off.
    """
    try:
        shopping = await _get_service().get_shopping_list()
    except Exception as e:
        raise ToolError(f"Failed to get the shopping list: {e}") from e

    items = [*shopping.ingredients, *shopping.additional]
    if not (shopping.recipes or items):
        return "The shopping list is empty."
    lines: list[str] = []
    if shopping.recipes:
        lines += ["Recipes:", *(f"  - {r.name} | ID: {r.id}" for r in shopping.recipes), ""]
    sections = [
        ("To buy (recipe ingredients):", [i for i in shopping.ingredients if not i.is_owned]),
        ("To buy (own items):", [i for i in shopping.additional if not i.is_owned]),
        ("Already have:", [i for i in items if i.is_owned]),
    ]
    for title, section in sections:
        if section:
            lines += [title, *map(_item_line, section), ""]
    return "\n".join(lines).strip()


@mcp.tool(annotations=WRITE)
async def add_recipes_to_shopping_list(recipe_ids: list[str] | str) -> str:
    """
    Add the ingredients of recipes to the Cookidoo shopping list.

    Works with catalogue recipes (IDs like "r59322") and custom recipes.

    Args:
        recipe_ids: Recipe IDs, as a list or comma-separated
    """
    ids = _ids(recipe_ids)
    try:
        added = await _get_service().add_recipes_to_shopping_list(ids)
    except Exception as e:
        raise ToolError(f"Failed to add recipes to the shopping list: {e}") from e
    return f"Added {len(ids)} recipe(s) to the shopping list ({len(added)} ingredients)."


@mcp.tool(annotations=DESTRUCTIVE)
async def remove_recipes_from_shopping_list(recipe_ids: list[str] | str) -> str:
    """
    Remove recipes and their ingredients from the Cookidoo shopping list.

    Args:
        recipe_ids: Recipe IDs, as shown by get_shopping_list
    """
    ids = _ids(recipe_ids)
    try:
        await _get_service().remove_recipes_from_shopping_list(ids)
    except Exception as e:
        raise ToolError(f"Failed to remove recipes from the shopping list: {e}") from e
    return f"Removed {len(ids)} recipe(s) from the shopping list."


@mcp.tool(annotations=WRITE)
async def add_shopping_items(items: list[str] | str) -> str:
    """
    Add free-text items (not from a recipe) to the Cookidoo shopping list,
    e.g. "kitchen roll" or "2 lemons".

    Args:
        items: The items, as a list (or one per line)
    """
    names = _lines(items)
    if not names:
        raise ToolError("Provide at least one item.")
    try:
        added = await _get_service().add_shopping_items(names)
    except Exception as e:
        raise ToolError(f"Failed to add items to the shopping list: {e}") from e
    return "Added to the shopping list:\n" + "\n".join(map(_item_line, added))


@mcp.tool(annotations={**WRITE, "idempotentHint": True})
async def mark_shopping_items(item_ids: list[str] | str, owned: bool = True) -> str:
    """
    Tick shopping list items off as bought / already owned, or untick them.

    Works for recipe ingredients and own items.

    Args:
        item_ids: Item IDs, as shown by get_shopping_list
        owned: True to tick the items off, False to put them back on the list
    """
    ids = _ids(item_ids)
    try:
        edited = await _get_service().set_items_owned(ids, owned)
    except Exception as e:
        raise ToolError(f"Failed to update the shopping list: {e}") from e
    state = "ticked off" if owned else "put back on the list"
    return f"{len(edited)} item(s) {state}:\n" + "\n".join(map(_item_line, edited))


@mcp.tool(annotations={**WRITE, "idempotentHint": True})
async def rename_shopping_item(item_id: str, name: str) -> str:
    """
    Rename one of the user's own shopping list items (recipe ingredients cannot be renamed).

    Args:
        item_id: The item ID, as shown by get_shopping_list
        name: The new text of the item
    """
    if not name.strip():
        raise ToolError("Provide the new name.")
    try:
        item = await _get_service().rename_shopping_item(item_id.strip(), name.strip())
    except Exception as e:
        raise ToolError(f"Failed to rename the item: {e}") from e
    return "Item renamed:\n" + _item_line(item)


@mcp.tool(annotations=DESTRUCTIVE)
async def remove_shopping_items(item_ids: list[str] | str) -> str:
    """
    Remove the user's own items from the Cookidoo shopping list. Recipe ingredients
    cannot be removed one by one: tick them off with mark_shopping_items, or remove
    their recipe with remove_recipes_from_shopping_list.

    Args:
        item_ids: Item IDs, as shown by get_shopping_list
    """
    ids = _ids(item_ids)
    try:
        await _get_service().remove_shopping_items(ids)
    except Exception as e:
        raise ToolError(f"Failed to remove items: {e}") from e
    return f"Removed {len(ids)} item(s) from the shopping list."


@mcp.tool(annotations=DESTRUCTIVE)
async def clear_shopping_list() -> str:
    """
    Empty the Cookidoo shopping list: all recipes, ingredients and own items.

    Only call this after the user explicitly confirmed it.
    """
    try:
        await _get_service().clear_shopping_list()
    except Exception as e:
        raise ToolError(f"Failed to clear the shopping list: {e}") from e
    return "The shopping list is now empty."


@mcp.tool(annotations=WRITE)
async def shop_for_meal_plan(week_of: str = "") -> str:
    """
    Add every recipe planned in a week of the meal plan to the shopping list.
    Recipes already on the shopping list are skipped.

    Args:
        week_of: Any date in the week, as YYYY-MM-DD (default: today)
    """
    day = _parse_day(week_of)
    service = _get_service()
    try:
        days = await service.get_calendar_week(day)
        on_list = {recipe.id for recipe in (await service.get_shopping_list()).recipes}
        to_add = [i for i in _week_recipe_ids(days) if i not in on_list]
        added = await service.add_recipes_to_shopping_list(to_add) if to_add else []
    except Exception as e:
        raise ToolError(f"Failed to shop for the meal plan: {e}") from e

    skipped = len(_week_recipe_ids(days)) - len(to_add)
    if not to_add:
        if skipped:
            return "Every recipe of that week is already on the shopping list."
        return f"Nothing planned in the week of {day.isoformat()}."
    text = f"Added {len(to_add)} recipe(s) to the shopping list ({len(added)} ingredients)."
    if skipped:
        text += f" {skipped} recipe(s) were already on it."
    return text


@mcp.prompt
def plan_my_week(days: int = 7, preferences: str = "", start: str = "") -> str:
    """
    Guided workflow to plan meals on the Cookidoo calendar and fill the shopping list.

    Args:
        days: Number of days to plan
        preferences: Diet, dislikes, time limits, ingredients to use up...
        start: First day, as YYYY-MM-DD (default: today)
    """
    first = start or date.today().isoformat()
    wishes = f" Preferences: {preferences}." if preferences else ""
    return (
        f"Help me plan {days} days of meals on Cookidoo, starting {first}.{wishes}\n\n"
        "1. Check what is already planned with get_meal_plan, and what is on the shopping "
        "list with get_shopping_list.\n"
        "2. Find recipes with search_recipes (and list_custom_recipes for my own), with "
        "some variety and dishes that share ingredients.\n"
        "3. Show me the plan, one line per day, and wait for my approval or changes.\n"
        "4. Once I approve, add the recipes day by day with add_to_meal_plan.\n"
        "5. Ask whether to add them to the shopping list; if yes, use shop_for_meal_plan "
        "(once per calendar week covered), then show the list with get_shopping_list."
    )


@mcp.prompt
def create_thermomix_recipe(dish: str, language: str = "en") -> str:
    """
    Guided workflow to write a Thermomix recipe for a dish and upload it to Cookidoo.

    Args:
        dish: The dish to create, e.g. "Creamy mushroom and parmesan risotto"
        language: "en" or "fr"
    """
    filename = "prompt_FR.md" if language.lower().startswith("fr") else "prompt.md"
    template = (PROMPTS_DIR / filename).read_text(encoding="utf-8")
    return re.sub(r"\[[^\]]*\]", f'"{dish}"', template, count=1)


if __name__ == "__main__":
    mcp.run()
