from typing import Optional
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from urllib.parse import urlparse
import httpx
import json
import logging
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# Add CORS middleware to allow client to connect locally
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods
    allow_headers=["*"],  # Allow all headers
)

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Pydantic models for request validation
class DetailsRequest(BaseModel):
    day: str
    party_size: int
    config_token: str
    restaurant_id: str
    headers: dict
    select_proxy: Optional[dict] = None

class ReservationRequest(BaseModel):
    book_token: str
    payment_id: int
    headers: dict
    select_proxy: Optional[dict] = None

def format_proxy_url(proxy_url: str) -> str:
    if not urlparse(proxy_url).scheme:
        return f"http://{proxy_url}"
    return proxy_url

@app.get("/")
async def index():
    logger.info("Index route accessed")
    return {"message": "Server is live!"}

@app.post("/api/get-details")
async def get_details(data: DetailsRequest):
    logger.info("Get details endpoint accessed")
    logger.debug(f"Request received for restaurant {data.restaurant_id}, day {data.day}")

    # Format proxy URLs
    formatted_proxies = None
    if data.select_proxy:
        formatted_proxies = {}
        for scheme, proxy in data.select_proxy.items():
            formatted_proxies[f"{scheme}://"] = format_proxy_url(proxy)
        formatted_proxies['https://'] = formatted_proxies.get('http://', formatted_proxies.get('https://', ''))

    url = f'https://api.resy.com/3/details?day={data.day}&party_size={data.party_size}&x-resy-auth-token={data.headers["X-Resy-Auth-Token"]}&venue_id={data.restaurant_id}&config_id={data.config_token}'
    headers = {
        'Accept': '*/*',
        'Authorization': data.headers["Authorization"],
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Origin': 'https://resy.com',
        'Referer': 'https://resy.com/',
    }

    async with httpx.AsyncClient(proxies=formatted_proxies, verify=False) as client:
        try:
            response = await client.get(url, headers=headers)
            logger.info(f"Get Details API request made for restaurant {data.restaurant_id} using proxy {formatted_proxies}")
            logger.debug(f"Response status code: {response.status_code}")
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            logger.warning(f"Resy API error for restaurant {data.restaurant_id}: {e.response.status_code}")
            raise HTTPException(status_code=e.response.status_code, detail=f"Failed to get details for restaurant {data.restaurant_id}")
        except httpx.ProxyError as e:
            logger.error(f"Proxy error: {e}")
            raise HTTPException(status_code=500, detail="Proxy connection failed")
        except httpx.RequestError as e:
            logger.error(f"Request failed: {e}")
            raise HTTPException(status_code=500, detail="Request failed")

    response_data = response.json()
    logger.info("Details retrieved successfully")
    return {"response_value": response_data['book_token']['value']}

@app.post("/api/book-reservation")
async def book_reservation(data: ReservationRequest):
    logger.info("Book reservation endpoint accessed")
    logger.debug(f"Booking request received for payment_id {data.payment_id}")

    # Format proxy URLs
    formatted_proxies = None
    if data.select_proxy:
        formatted_proxies = {}
        for scheme, proxy in data.select_proxy.items():
            formatted_proxies[f"{scheme}://"] = format_proxy_url(proxy)
        formatted_proxies['https://'] = formatted_proxies.get('http://', formatted_proxies.get('https://', ''))

    url = 'https://api.resy.com/3/book'
    payload = {
        'book_token': data.book_token,
        'struct_payment_method': json.dumps({"id": data.payment_id}),
        'source_id': 'resy.com-venue-details',
    }

    headers = {
        'X-Origin': 'https://widgets.resy.com',
        'X-Resy-Auth-Token': data.headers['X-Resy-Auth-Token'],
        'Authorization': data.headers['Authorization'],
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'X-Resy-Universal-Auth': data.headers['X-Resy-Auth-Token'],
        'Accept': 'application/json, text/plain, */*',
        'Origin': 'https://widgets.resy.com',
        'Referer': 'https://widgets.resy.com/',
        'Content-Type': 'application/x-www-form-urlencoded',
    }

    async with httpx.AsyncClient(proxies=formatted_proxies, verify=False) as client:
        response = await client.post(url, data=payload, headers=headers)

    logger.info(f"Reservation request made. Status code: {response.status_code} using proxy {formatted_proxies}")
    return JSONResponse(content=response.json(), status_code=response.status_code)

if __name__ == '__main__':
    import uvicorn
    logger.info("Starting FastAPI application")
    uvicorn.run(app, host="0.0.0.0", port=8000)
