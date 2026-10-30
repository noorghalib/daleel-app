"""Minimal Flask integration example; it is not the Daleel backend itself.

Install Flask separately if you want to run this optional example:
    python -m pip install flask
The model is loaded once on process startup, not once per HTTP request.
"""

from flask import Flask, jsonify, request
import pandas as pd

from daleel_ml import load_model


app = Flask(__name__)
MODEL = load_model()  # Load the trained artifact once when the app starts.


@app.post("/predict")
def predict_transactions():
    payload = request.get_json(force=True)
    rows = payload if isinstance(payload, list) else payload.get("transactions", [])
    predictions = MODEL.predict(pd.DataFrame(rows))
    return jsonify(predictions.to_dict(orient="records"))


if __name__ == "__main__":
    app.run(debug=False)
