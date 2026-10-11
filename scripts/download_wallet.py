"""Download only the two files needed for python-oracledb Thin mTLS."""

import getpass
import io
import os
import sys
import zipfile
from pathlib import Path

import oci
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]


def main():
    load_dotenv(ROOT / ".env")
    database_id = os.environ["OCI_DB_OCID"]
    wallet_password = os.getenv("DB_WALLET_PASSWORD") or getpass.getpass(
        "Choose a new wallet password (save it as DB_WALLET_PASSWORD in .env): "
    )
    if len(wallet_password) < 16:
        raise ValueError("Choose a wallet password of at least 16 characters")
    directory = Path(os.environ["DB_WALLET_DIR"]).expanduser().resolve()
    if (directory / "ewallet.pem").exists() or (directory / "tnsnames.ora").exists():
        raise FileExistsError("Wallet files already exist; move them before downloading again")
    config = oci.config.from_file(
        file_location=os.path.expanduser(os.getenv("OCI_CONFIG_FILE", "~/.oci/config")),
        profile_name=os.getenv("OCI_PROFILE", "DEFAULT"),
    )
    details = oci.database.models.GenerateAutonomousDatabaseWalletDetails(
        password=wallet_password, generate_type="SINGLE"
    )
    response = oci.database.DatabaseClient(config).generate_autonomous_database_wallet(
        database_id, details
    )
    archive = response.data
    if hasattr(archive, "content"):
        archive = archive.content
    if hasattr(archive, "read"):
        archive = archive.read()
    os.umask(0o077)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as wallet:
        for name in ("tnsnames.ora", "ewallet.pem"):
            data = wallet.read(name)
            path = directory / name
            with path.open("xb") as output:
                output.write(data)
            path.chmod(0o600)
    print(f"Wallet files saved in {directory}; keep the wallet password private.")


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, FileExistsError, zipfile.BadZipFile,
            oci.exceptions.ServiceError) as exc:
        print(f"Wallet download failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
