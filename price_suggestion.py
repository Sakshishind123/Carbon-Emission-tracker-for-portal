from app import db, Products  # Importing from your existing app
import ollama

def get_competitor_prices(product_name):
    """Fetch competitor prices from the database"""
    products = Products.query.filter_by(product_name=product_name).all()
    return [product.price for product in products]  # Ensure correct column name

def suggest_dynamic_price(product_name, farmer_price):
    """Use AI to suggest a competitive price"""
    products = Products.query.filter_by(product_name=product_name).all()
    
    if not products:
        return farmer_price  # No competitors, return farmer's price

    competitor_prices = [product.price for product in products]

    avg_price = sum(competitor_prices) / len(competitor_prices)
    lowest_price = min(competitor_prices)

    prompt = f"""
    The current lowest price for {product_name} is ₹{lowest_price}/kg.
    The average price is ₹{avg_price}/kg.
    A new farmer wants to set a price of ₹{farmer_price}/kg.
    Suggest an optimal price that is competitive but profitable.
    """

    response = ollama.chat(model='llama3', messages=[{'role': 'user', 'content': prompt}])

    if 'choices' in response and response['choices']:
        suggested_price = response['choices'][0].get('message', {}).get('content', str(farmer_price))
    else:
        suggested_price = str(farmer_price)  # Default to farmer's price in case of API failure

    return suggested_price
