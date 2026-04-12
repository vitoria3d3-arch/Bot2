import requests

def check_symbol():
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    r = requests.get(url)
    symbols = r.json()['symbols']
    for s in symbols:
        if s['symbol'] == 'BTCUSDC':
            print(f"Symbol: {s['symbol']}")
            print(f"Price Precision: {s['pricePrecision']}")
            print(f"Quantity Precision: {s['quantityPrecision']}")
            for f in s['filters']:
                if f['filterType'] == 'LOT_SIZE':
                    print(f"Min Qty: {f['minQty']}")
                    print(f"Step Size: {f['stepSize']}")
            return
    print("Symbol not found")

if __name__ == "__main__":
    check_symbol()
