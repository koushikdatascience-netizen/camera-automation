from __future__ import annotations
import argparse, hashlib, os, secrets
from cloud_portal.postgres_storage import PostgresPortalStore

def main():
    p=argparse.ArgumentParser(description="Provision or rotate a scoped SnapKey edge credential")
    p.add_argument("--tenant",required=True); p.add_argument("--company",default=None)
    p.add_argument("--shop",required=True); p.add_argument("--site",required=True); p.add_argument("--edge",required=True)
    p.add_argument("--rotate",action="store_true")
    a=p.parse_args()
    store=PostgresPortalStore(os.environ.get("SNAPKEY_DATABASE_URL"))
    if a.rotate: store.revoke_edge_credentials(a.tenant,a.shop,a.edge)
    token=secrets.token_urlsafe(32)
    store.provision_edge_credential(hashlib.sha256(token.encode()).hexdigest(),a.tenant,a.company,a.shop,a.site,a.edge)
    print("EDGE_API_TOKEN="+token)
    print("Store this token only on the assigned edge. The cloud database stores only its SHA-256 digest.")

if __name__=="__main__": main()
