# FreshSave AI Backend Starter

This is the initial shared backend for the FreshSave AI retailer and customer websites.

## Included
- FastAPI app with Swagger docs
- MongoDB Atlas connection using PyMongo's async client
- `/` and `/health` endpoints
- Initial product catalogue endpoints:
  - `POST /api/products`
  - `GET /api/products`
  - `GET /api/products/{product_id}`
  - `DELETE /api/products/{product_id}` (soft-deactivates)
- Local CORS origins for Vite development servers
- `.env.example` and `.gitignore`

## Before running
1. Install Python 3.11 or newer.
2. Create a MongoDB Atlas database user and allow your current IP in Network Access.
3. Copy `.env.example` to `.env`.
4. Put your real MongoDB connection string in `.env`.
5. Keep `.env` private and never commit it to Git.

If your MongoDB password contains special characters, URL-encode the password portion of the URI.

## Windows PowerShell setup
Run these commands from the extracted `FreshSave-AI-Backend` folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and replace the placeholder connection string with the real Atlas URI. Then run:

```powershell
uvicorn app.main:app --reload
```

Open:
- http://127.0.0.1:8000/
- http://127.0.0.1:8000/health
- http://127.0.0.1:8000/docs

The API starts only if it can connect to MongoDB. If startup fails, check the URI, database user's password, Atlas IP access list, and network connection.

## Test product creation
In Swagger, open `POST /api/products`, click Try it out, and use sample JSON:

```json
{
  "name": "Whole Wheat Bread",
  "category": "Bakery",
  "unit_cost": 25,
  "base_price": 40,
  "shelf_life_days": 5,
  "average_daily_demand": 12,
  "price_sensitivity": 1.2,
  "description": "Demo product for local development",
  "image_url": null,
  "active": true
}
```

Then call `GET /api/products` to confirm it was saved.

## Important development limitations
- This is a starter foundation, not the complete production backend.
- Authentication and role-based authorization are not implemented yet. Do not expose these endpoints publicly or deploy them for real use until authentication and authorization are added.
- Product creation is not yet tied to a store or retailer identity.
- Inventory batches, orders, customer carts, pricing recommendations, approved deals, and the required 30-day simulation are future modules.
- Simulation data must be kept separate from live stock, prices, and orders.
- Customer-facing routes must eventually expose only approved deals and available inventory.
- Add tests, indexes, request limits, secure deployment configuration, and proper audit logging before production.
