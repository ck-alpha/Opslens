"""Independent product configuration; never reads evaluation candidates or truth."""

from pathlib import Path

from opslens.product.models import DemoAnalysisRecipe


def load_recipes(path: Path) -> dict[str, DemoAnalysisRecipe]:
    recipes = {}
    for file in sorted(path.glob("*.json")):
        recipe = DemoAnalysisRecipe.model_validate_json(file.read_text("utf-8"))
        if recipe.recipe_id in recipes:
            raise ValueError("DUPLICATE_PRODUCT_RECIPE")
        recipes[recipe.recipe_id] = recipe
    if not recipes:
        raise ValueError("EMPTY_PRODUCT_CATALOG")
    return recipes
