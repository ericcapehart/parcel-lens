# Parcel Lens (Utah prototype)

Point your phone at a house, tap the button, and get its listing status and details. The address comes from GPS; the photo is the backdrop for the result.

## Run

```
python3 server.py            # http://localhost:8000
```

Optional listing feeds (environment variables):

| Variable | What it adds |
|---|---|
| `WFRMLS_TOKEN` | UtahRealEstate.com RESO Web API: live status, list price, beds and baths, remarks. Requires a data license from UtahRealEstate.com. |
| `RENTCAST_API_KEY` | RentCast: active and inactive sale listings plus public-record beds and baths. Has a self-serve key and a free tier. |

Without either variable, the app shows the county record data and reports the status as **Unknown**.

## On a phone

Browsers allow camera and GPS only over HTTPS. To test on a phone, put the server behind an HTTPS tunnel, for example:

```
cloudflared tunnel --url http://localhost:8000
```

## Deploy (Render + parcels.ericcapehart.com)

1. Push this folder to a GitHub repository.
2. In Render, choose **New → Blueprint**, select the repo, and Render reads `render.yaml`. Leave the API keys blank for now.
3. In the service's **Settings → Custom Domains**, add `parcels.ericcapehart.com`.
4. In Namecheap, open **Advanced DNS** and add a **CNAME** record with host `parcels` pointing to `<your-service>.onrender.com`.
5. Wait for Render to show the domain as verified and the certificate as issued.

## Data flow

1. GPS coordinates go to UGRC's county and ZIP code boundary layers to get the county, city and ZIP code. Typed addresses are first converted to coordinates by the U.S. Census geocoder. Neither service needs a key.
2. The county's UGRC LIR parcel layer returns all parcels within 60 m, and the app picks the nearest one. The other nearby parcels appear under "Not this one?"
3. The listing feeds are checked in priority order: WFRMLS first, then RentCast.
4. The results are merged. Listing data takes priority over county assessor data.
