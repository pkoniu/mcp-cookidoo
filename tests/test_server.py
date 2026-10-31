import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cookidoo_api import (
    Cookidoo,
    CookidooAdditionalItem,
    CookidooIngredientItem,
    CookidooLocalizationConfig,
    CookidooShoppingRecipe,
)
from cookidoo_api.types import CookidooCalendarDay, CookidooCalendarDayRecipe
from fastmcp import Client
from fastmcp.exceptions import ToolError
from yarl import URL

import server
from cookidoo_service import (
    CookidooService,
    CookidooSettings,
    ShoppingList,
    load_cookidoo_settings,
    split_recipe_ids,
)
from guided import to_instruction
from schemas import CustomRecipe, Step

RECIPE = {
    "name": "Cookies",
    "ingredients": ["200 g flour", "100 g butter"],
    "steps": ["Mix 20 sec/speed 4", "Bake 12 min at 180°C"],
    "servings": 6,
    "prep_time": 15,
    "total_time": 30,
    "hints": None,
}


@pytest.fixture
def service(monkeypatch):
    svc = CookidooService(CookidooSettings(email="a@b.c", password="x"))
    for name in (
        "login",
        "search_recipes",
        "get_recipe_details",
        "list_custom_recipes",
        "get_custom_recipe",
        "create_custom_recipe",
        "update_custom_recipe",
        "get_calendar_week",
        "add_to_calendar",
        "remove_from_calendar",
        "get_shopping_list",
        "add_recipes_to_shopping_list",
        "remove_recipes_from_shopping_list",
        "add_shopping_items",
        "rename_shopping_item",
        "set_items_owned",
        "remove_shopping_items",
        "clear_shopping_list",
    ):
        monkeypatch.setattr(svc, name, AsyncMock())
    monkeypatch.setattr(server, "_cookidoo_service", svc)
    return svc


async def call(tool, **args):
    async with Client(server.mcp) as client:
        result = await client.call_tool(tool, args)
        return result.content[0].text


async def test_all_tools_registered():
    async with Client(server.mcp) as client:
        names = {t.name for t in await client.list_tools()}
        prompts = {p.name for p in await client.list_prompts()}
    assert names == {
        "connect_to_cookidoo",
        "search_recipes",
        "get_recipe_details",
        "list_custom_recipes",
        "get_custom_recipe",
        "generate_recipe_structure",
        "upload_custom_recipe",
        "update_custom_recipe",
        "get_meal_plan",
        "add_to_meal_plan",
        "remove_from_meal_plan",
        "get_shopping_list",
        "add_recipes_to_shopping_list",
        "remove_recipes_from_shopping_list",
        "add_shopping_items",
        "mark_shopping_items",
        "rename_shopping_item",
        "remove_shopping_items",
        "clear_shopping_list",
        "shop_for_meal_plan",
    }
    assert prompts == {"create_thermomix_recipe", "plan_my_week"}


async def test_server_reports_project_version():
    async with Client(server.mcp) as client:
        assert client.server_info.version == server.VERSION == "1.2.0"


async def test_generate_recipe_structure_round_trips_to_upload(service):
    service.create_custom_recipe.return_value = SimpleNamespace(
        name="Cookies", id="01ABC", url="https://cookidoo.fr/created-recipes/fr-FR/01ABC"
    )
    generated = await call(
        "generate_recipe_structure",
        name="Cookies",
        ingredients="200 g flour\n100 g butter",
        steps="1. Mix 20 sec/speed 4\n2) Bake 12 min at 180°C",
        servings=6,
        prep_time=15,
        total_time=30,
    )
    assert json.loads(generated) == RECIPE

    text = await call("upload_custom_recipe", recipe_json=generated)
    assert "01ABC" in text
    service.create_custom_recipe.assert_awaited_once_with(CustomRecipe(**RECIPE))


async def test_generate_keeps_commas_and_accepts_lists(service):
    generated = await call(
        "generate_recipe_structure",
        name="Quiche",
        ingredients=["300 g potatoes, sliced", "2 eggs"],
        steps="1. Slice 5 sec/speed 5\n- Bake 30 min, 180°C",
        hints="Serve warm, with salad",
    )
    recipe = json.loads(generated)
    assert recipe["ingredients"] == ["300 g potatoes, sliced", "2 eggs"]
    assert recipe["steps"] == ["Slice 5 sec/speed 5", "Bake 30 min, 180°C"]
    assert recipe["hints"] == ["Serve warm, with salad"]


async def test_prep_time_cannot_exceed_total_time(service):
    with pytest.raises(ToolError, match="prep_time"):
        await call(
            "generate_recipe_structure",
            name="x",
            ingredients="a",
            steps="b",
            prep_time=40,
            total_time=30,
        )


async def test_upload_rejects_invalid_json(service):
    with pytest.raises(ToolError, match="Invalid recipe data"):
        await call("upload_custom_recipe", recipe_json="{not json")
    service.create_custom_recipe.assert_not_awaited()


async def test_search_splits_ingredients(service):
    service.search_recipes.return_value = SimpleNamespace(
        total=1, recipes=[SimpleNamespace(name="Gratin", id="r1", url="https://x/r1")]
    )
    text = await call("search_recipes", ingredients="chicken, zucchini")
    assert "Gratin" in text and "r1" in text
    assert service.search_recipes.await_args.kwargs["ingredients"] == ["chicken", "zucchini"]


async def test_search_requires_criteria(service):
    with pytest.raises(ToolError):
        await call("search_recipes")


async def test_get_recipe_details_formats_steps_and_times(service):
    service.get_recipe_details.return_value = SimpleNamespace(
        id="r1",
        name="Soup",
        serving_size=4,
        active_time=600,
        total_time=1800,
        difficulty="easy",
        utensils=[],
        ingredients=[SimpleNamespace(name="carrots", description="300 g", preparation="sliced")],
        step_groups=[
            SimpleNamespace(
                title="", recipe_steps=[SimpleNamespace(formatted_text="Cook <b>20 min</b>")]
            )
        ],
        notes=[],
        url="https://x/r1",
    )
    text = await call("get_recipe_details", recipe_id="r1")
    assert "Total time: 30 min" in text
    assert "  - 300 g carrots, sliced" in text
    assert "1. Cook 20 min" in text


def custom_recipe(steps, ingredients):
    """What CookidooService.get_custom_recipe returns: the recipe and its raw steps."""
    recipe = SimpleNamespace(
        name="Soupe",
        ingredients=ingredients,
        serving_size=6,
        active_time=900,
        total_time=1800,
        hints=[],
        url="https://x",
    )
    instructions = [to_instruction(step, "fr-FR") for step in steps]
    return recipe, Cookidoo._process_recipe_steps(instructions, ingredients)


async def test_get_custom_recipe_returns_editable_json(service):
    recipe, raw = custom_recipe(RECIPE["steps"], RECIPE["ingredients"])
    recipe.name = "Cookies"
    service.get_custom_recipe.return_value = (recipe, raw)
    text = await call("get_custom_recipe", recipe_id="01ABC")
    assert json.loads(text.split("\n\n", 1)[1]) == RECIPE


SAUTE = {
    "text": "Faire revenir l'oignon {settings}.",
    "settings": [
        {"mode": "manual", "time": 180, "temperature": "120", "speed": "1", "reverse": False}
    ],
    "ingredients": [{"text": "l'oignon", "ingredient": "1 oignon"}],
}
KNEAD = {
    "text": "Pétrir {settings}.",
    "settings": [{"mode": "dough", "time": 120}],
    "ingredients": [],
}


async def test_generate_accepts_structured_steps(service):
    generated = await call(
        "generate_recipe_structure",
        name="Soupe",
        ingredients=["1 oignon"],
        steps=[
            "1. Éplucher l'oignon.",
            SAUTE,
            {"text": "Pétrir {settings}.", "settings": [{"mode": "dough", "time": 120}]},
        ],
    )
    assert json.loads(generated)["steps"] == ["Éplucher l'oignon.", SAUTE, KNEAD]


@pytest.mark.parametrize(
    "steps",
    [
        [{"text": "Mixer {settings}.", "settings": [{"time": 10, "speed": "12"}]}],
        [{"text": "Mixer {settings}.", "settings": [{"mode": "sous_vide", "time": 10}]}],
        [{"text": "Ajouter le sel.", "ingredients": [{"text": "sel", "ingredient": "sel"}]}],
    ],
)
async def test_generate_rejects_invalid_steps(service, steps):
    with pytest.raises(ToolError):
        await call("generate_recipe_structure", name="Soupe", ingredients=["1 oignon"], steps=steps)


async def test_get_custom_recipe_round_trips_structured_steps(service):
    steps = ["Éplucher.", Step(**SAUTE), Step(**KNEAD)]
    service.get_custom_recipe.return_value = custom_recipe(steps, ["1 oignon"])
    read = (await call("get_custom_recipe", recipe_id="01ABC")).split("\n\n", 1)[1]
    generated = await call(
        "generate_recipe_structure", name="Soupe", ingredients=["1 oignon"], steps=steps
    )
    assert (
        json.loads(read)["steps"] == json.loads(generated)["steps"] == ["Éplucher.", SAUTE, KNEAD]
    )


async def test_service_reads_raw_steps(monkeypatch):
    raw = {
        "recipeId": "01ABC",
        "recipeContent": {
            "name": "Soupe",
            "ingredients": [{"type": "INGREDIENT", "text": "1 oignon"}],
            "instructions": [{"type": "STEP", "text": "Turbo/0.5 sec/3 fois"}],
            "yield": {"value": 4, "unitText": "portion"},
            "prepTime": 600,
            "totalTime": 1800,
        },
    }
    request = AsyncMock(return_value=raw)
    api = SimpleNamespace(
        _ensure_endpoints=AsyncMock(),
        api_endpoint=URL("https://cookidoo.fr"),
        _path=lambda name: "created-recipes/{language}/{id}",
        localization=CookidooLocalizationConfig(country_code="fr", language="fr-FR"),
        _request_json=request,
    )
    svc = CookidooService(CookidooSettings(email="a@b.c", password="x"))
    monkeypatch.setattr(svc, "client", AsyncMock(return_value=api))
    recipe, steps = await svc.get_custom_recipe("01ABC")
    assert recipe.name == "Soupe"
    assert steps == raw["recipeContent"]["instructions"]
    assert str(request.await_args.args[1]) == "https://cookidoo.fr/created-recipes/fr-FR/01ABC"
    assert "customer-recipe.full" in request.await_args.kwargs["headers"]["ACCEPT"]


async def test_service_sends_settings_in_account_language(monkeypatch):
    api = SimpleNamespace(create_custom_recipe=AsyncMock(), update_custom_recipe=AsyncMock())
    svc = CookidooService(CookidooSettings(email="a@b.c", password="x", language="fr-FR"))
    monkeypatch.setattr(svc, "client", AsyncMock(return_value=api))
    recipe = CustomRecipe(name="Soupe", ingredients=["1 oignon"], steps=["Éplucher.", SAUTE])
    await svc.create_custom_recipe(recipe)
    await svc.update_custom_recipe("01ABC", recipe)
    expected = ["Éplucher.", to_instruction(Step(**SAUTE), "fr-FR")]
    assert expected[1].text == "Faire revenir l'oignon 3 min/120°C/vitesse 1."
    assert api.create_custom_recipe.await_args.args[0].instructions == expected
    assert api.update_custom_recipe.await_args.args[1].instructions == expected


async def test_prompt_inserts_dish():
    async with Client(server.mcp) as client:
        result = await client.get_prompt(
            "create_thermomix_recipe", {"dish": "Risotto", "language": "fr"}
        )
    text = result.messages[0].content.text
    assert '"Risotto"' in text and "[" not in text.splitlines()[2]


def test_settings_from_env(monkeypatch):
    monkeypatch.setattr("cookidoo_service.load_dotenv", lambda: None)
    monkeypatch.setenv("COOKIDOO_EMAIL", "a@b.c")
    monkeypatch.setenv("COOKIDOO_PASSWORD", "x")
    monkeypatch.setenv("COOKIDOO_DEVICE", "tm7")
    assert load_cookidoo_settings().device.value == "TM7"

    monkeypatch.setenv("COOKIDOO_DEVICE", "TM99")
    with pytest.raises(ValueError, match="COOKIDOO_DEVICE"):
        load_cookidoo_settings()

    monkeypatch.delenv("COOKIDOO_PASSWORD")
    with pytest.raises(ValueError, match="Missing"):
        load_cookidoo_settings()


def day_recipe(recipe_id, name, is_custom=False):
    return CookidooCalendarDayRecipe(
        id=recipe_id,
        name=name,
        total_time=1800,
        thumbnail=None,
        image=None,
        url=f"https://x/{recipe_id}",
        is_custom=is_custom,
    )


def calendar_day(day_id, recipes=(), customer_recipe_ids=()):
    return CookidooCalendarDay(
        id=day_id,
        title=day_id,
        recipes=list(recipes),
        customer_recipe_ids=list(customer_recipe_ids),
    )


def ingredient(item_id, name, owned=False, description="", preparation=None):
    return CookidooIngredientItem(
        id=item_id, name=name, is_owned=owned, description=description, preparation=preparation
    )


def shopping_recipe(recipe_id, name):
    return CookidooShoppingRecipe(
        id=recipe_id, name=name, ingredients=[], thumbnail=None, image=None, url=""
    )


async def test_get_meal_plan_lists_days(service):
    service.get_calendar_week.return_value = [
        calendar_day(
            "2026-11-02",
            [day_recipe("r1", "Soup"), day_recipe("01ABC", "My pie", is_custom=True)],
            customer_recipe_ids=["01ABC", "01GONE"],
        ),
        calendar_day("2026-11-03"),
    ]
    text = await call("get_meal_plan", week_of="2026-11-04")
    service.get_calendar_week.assert_awaited_once_with(date(2026, 11, 4))
    assert "  - Soup (30 min) | ID: r1" in text
    assert "  - My pie (30 min, custom) | ID: 01ABC" in text
    assert "could not be loaded | ID: 01GONE" in text
    assert "2026-11-03:\n  (nothing planned)" in text


async def test_get_meal_plan_defaults_to_today_and_rejects_bad_dates(service):
    service.get_calendar_week.return_value = []
    await call("get_meal_plan")
    service.get_calendar_week.assert_awaited_once_with(date.today())
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        await call("get_meal_plan", week_of="next monday")


async def test_add_to_meal_plan_accepts_comma_separated_ids(service):
    service.add_to_calendar.return_value = calendar_day("2026-11-02", [day_recipe("r1", "Soup")])
    text = await call("add_to_meal_plan", day="2026-11-02", recipe_ids="r1, 01ABC")
    service.add_to_calendar.assert_awaited_once_with(date(2026, 11, 2), ["r1", "01ABC"])
    assert "Soup" in text


async def test_remove_from_meal_plan(service):
    service.remove_from_calendar.return_value = calendar_day("2026-11-02")
    text = await call("remove_from_meal_plan", day="2026-11-02", recipe_id="r1")
    service.remove_from_calendar.assert_awaited_once_with(date(2026, 11, 2), "r1")
    assert "(nothing planned)" in text


async def test_get_shopping_list_groups_items(service):
    service.get_shopping_list.return_value = ShoppingList(
        recipes=[shopping_recipe("r1", "Soup")],
        ingredients=[
            ingredient("i1", "carrots", description="300 g", preparation="sliced"),
            ingredient("i2", "salt", owned=True),
        ],
        additional=[CookidooAdditionalItem(id="a1", name="kitchen roll", is_owned=False)],
    )
    text = await call("get_shopping_list")
    assert text == "\n".join(
        [
            "Recipes:",
            "  - Soup | ID: r1",
            "",
            "To buy (recipe ingredients):",
            "  - 300 g carrots, sliced | ID: i1",
            "",
            "To buy (own items):",
            "  - kitchen roll | ID: a1",
            "",
            "Already have:",
            "  - salt | ID: i2",
        ]
    )


async def test_get_shopping_list_empty(service):
    service.get_shopping_list.return_value = ShoppingList()
    assert await call("get_shopping_list") == "The shopping list is empty."


async def test_add_shopping_items_one_per_line(service):
    service.add_shopping_items.return_value = [
        CookidooAdditionalItem(id="a1", name="2 lemons", is_owned=False)
    ]
    text = await call("add_shopping_items", items="2 lemons\n")
    service.add_shopping_items.assert_awaited_once_with(["2 lemons"])
    assert "2 lemons | ID: a1" in text


async def test_mark_shopping_items(service):
    service.set_items_owned.return_value = [ingredient("i1", "salt", owned=False)]
    text = await call("mark_shopping_items", item_ids=["i1"], owned=False)
    service.set_items_owned.assert_awaited_once_with(["i1"], False)
    assert text.startswith("1 item(s) put back on the list")


async def test_shopping_tools_require_ids(service):
    with pytest.raises(ToolError, match="at least one"):
        await call("remove_shopping_items", item_ids=" , ")
    service.remove_shopping_items.assert_not_awaited()


async def test_clear_shopping_list(service):
    assert "empty" in await call("clear_shopping_list")
    service.clear_shopping_list.assert_awaited_once()


async def test_shop_for_meal_plan_skips_recipes_already_listed(service):
    service.get_calendar_week.return_value = [
        calendar_day("2026-11-02", [day_recipe("r1", "Soup"), day_recipe("r2", "Pie")]),
        calendar_day("2026-11-03", [day_recipe("r1", "Soup")], customer_recipe_ids=["01ABC"]),
    ]
    service.get_shopping_list.return_value = ShoppingList(recipes=[shopping_recipe("r2", "Pie")])
    service.add_recipes_to_shopping_list.return_value = [ingredient("i1", "x")] * 5
    text = await call("shop_for_meal_plan", week_of="2026-11-02")
    service.add_recipes_to_shopping_list.assert_awaited_once_with(["r1", "01ABC"])
    assert text == (
        "Added 2 recipe(s) to the shopping list (5 ingredients). 1 recipe(s) were already on it."
    )


async def test_shop_for_meal_plan_empty_week(service):
    service.get_calendar_week.return_value = [calendar_day("2026-11-02")]
    service.get_shopping_list.return_value = ShoppingList()
    assert "Nothing planned" in await call("shop_for_meal_plan")
    service.add_recipes_to_shopping_list.assert_not_awaited()


async def test_plan_my_week_prompt():
    async with Client(server.mcp) as client:
        result = await client.get_prompt(
            "plan_my_week", {"days": "5", "preferences": "vegetarian", "start": "2026-11-02"}
        )
    text = result.messages[0].content.text
    assert "5 days" in text and "2026-11-02" in text and "vegetarian" in text


def test_split_recipe_ids():
    assert split_recipe_ids(["r1", "01ABC", "r59322", "r1x"]) == (
        ["r1", "r59322"],
        ["01ABC", "r1x"],
    )


def service_with(api):
    svc = CookidooService(CookidooSettings(email="a@b.c", password="x"))
    svc.client = AsyncMock(return_value=api)
    return svc


async def test_service_routes_calendar_and_shopping_by_recipe_kind():
    api = SimpleNamespace(
        add_recipes_to_calendar=AsyncMock(return_value="catalogue day"),
        add_custom_recipes_to_calendar=AsyncMock(return_value="custom day"),
        remove_recipe_from_calendar=AsyncMock(),
        remove_custom_recipe_from_calendar=AsyncMock(),
        add_ingredient_items_for_recipes=AsyncMock(return_value=["a"]),
        add_ingredient_items_for_custom_recipes=AsyncMock(return_value=["b"]),
    )
    svc = service_with(api)
    day = date(2026, 11, 2)

    assert await svc.add_to_calendar(day, ["r1", "01ABC", "r2"]) == "custom day"
    api.add_recipes_to_calendar.assert_awaited_once_with(day, ["r1", "r2"])
    api.add_custom_recipes_to_calendar.assert_awaited_once_with(day, ["01ABC"])

    await svc.remove_from_calendar(day, "01ABC")
    api.remove_custom_recipe_from_calendar.assert_awaited_once_with(day, "01ABC")
    api.remove_recipe_from_calendar.assert_not_awaited()

    assert await svc.add_recipes_to_shopping_list(["01ABC", "r1"]) == ["a", "b"]
    api.add_ingredient_items_for_recipes.assert_awaited_once_with(["r1"])
    api.add_ingredient_items_for_custom_recipes.assert_awaited_once_with(["01ABC"])


async def test_service_sets_ownership_on_both_item_kinds():
    salt = ingredient("i1", "salt")
    pepper = ingredient("i2", "pepper")
    roll = CookidooAdditionalItem(id="a1", name="kitchen roll", is_owned=False)
    api = SimpleNamespace(
        get_ingredient_items=AsyncMock(return_value=[salt, pepper]),
        get_additional_items=AsyncMock(return_value=[roll]),
        edit_ingredient_items_ownership=AsyncMock(side_effect=lambda items: items),
        edit_additional_items_ownership=AsyncMock(side_effect=lambda items: items),
    )
    svc = service_with(api)

    edited = await svc.set_items_owned(["i1", "a1"], True)
    assert [(i.id, i.is_owned) for i in edited] == [("i1", True), ("a1", True)]
    assert api.edit_ingredient_items_ownership.await_args.args[0] == [
        ingredient("i1", "salt", owned=True)
    ]
    assert salt.is_owned is False  # the loaded items are not mutated

    with pytest.raises(ValueError, match="nope"):
        await svc.set_items_owned(["i1", "nope"], True)


async def test_service_renames_only_own_items():
    roll = CookidooAdditionalItem(id="a1", name="kitchen roll", is_owned=False)
    api = SimpleNamespace(
        get_additional_items=AsyncMock(return_value=[roll]),
        edit_additional_items=AsyncMock(side_effect=lambda items: items),
    )
    svc = service_with(api)
    renamed = await svc.rename_shopping_item("a1", "paper towels")
    assert renamed.name == "paper towels" and renamed.id == "a1"
    with pytest.raises(ValueError, match="i1"):
        await svc.rename_shopping_item("i1", "x")
