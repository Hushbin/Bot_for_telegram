Project Analysis Summary
Based on my analysis of the Telegram bot project, I've identified the following:

Current Technology Stack:
FaunaDB (NoSQL Database): Used for storing:
User information (name, email, telephone, chat_id)
SME owner status and preferences
The code uses FaunaDB's Python client with query operations (create, update, ref, collection)
Cloudinary (Image Storage): Used for:
Image/media upload functionality
Configuration includes cloud_name, api_key, api_secret
Database Operations Identified:
Creating user records in "User" collection
Updating user records (SME owner status)
Storing user context data (user-id, user-name, user-data)
Comprehensive Migration Plan: Oracle Cloud Solutions
Phase 1: Analysis & Architecture Design (Duration: 1-2 days)
1.1 Oracle Cloud Services Selection
Replace FaunaDB with Oracle Autonomous JSON Database:

Service: Oracle Autonomous JSON Database (part of Oracle Cloud Infrastructure)
Alternative: Oracle Autonomous Database with JSON collections
Rationale:
Native NoSQL document support with SODA (Simple Oracle Document Access)
Fully managed, auto-scaling
Enterprise-grade security and reliability
Cost-effective for small to medium workloads
Replace Cloudinary with Oracle Cloud Object Storage:

Service: Oracle Cloud Infrastructure (OCI) Object Storage
Rationale:
Highly durable (99.999999999% durability)
Unlimited scalability
Built-in image processing capabilities via OCI Vision service
Lower costs compared to Cloudinary
S3-compatible API available
1.2 Architecture Comparison
Current Architecture:

Telegram Bot → Python Handler → FaunaDB (User Data)
                              → Cloudinary (Images)
Target Oracle Architecture:

Telegram Bot → Python Handler → Oracle Autonomous JSON DB (User Data)
                              → OCI Object Storage (Images)
                              → OCI Vision API (Optional: Image processing)
Phase 2: Oracle Cloud Setup & Configuration (Duration: 2-3 days)
2.1 Oracle Cloud Account Setup
Create OCI Account
Sign up for Oracle Cloud Free Tier (includes Always Free resources)
Navigate to Oracle Cloud Console
Set Up Compartment
Create a dedicated compartment for the bot project
Configure IAM policies for resource access
2.2 Oracle Autonomous JSON Database Setup
Steps:

Create Database Instance
- Service: Oracle Autonomous JSON Database
- Workload Type: JSON
- Deployment Type: Shared Infrastructure (for cost optimization)
- Database Name: smebot_db
- Admin Password: [Secure password]
- Network Access: Secure access from allowed IPs/VCN
Configure Network Access
Set up access control lists (ACLs)
Download wallet file for secure connections
Configure mutual TLS (mTLS) authentication
Create Collections
Create "User" collection for user data
Create indexes for frequently queried fields (chat_id, email)
Sample SODA Schema
{
  "name": "User",
  "description": "User collection for SME Bot",
  "contentColumn": {
    "name": "JSON_DOCUMENT",
    "sqlType": "BLOB"
  },
  "keyColumn": {
    "name": "ID",
    "sqlType": "VARCHAR2(255)",
    "assignmentMethod": "UUID"
  },
  "creationTimeColumn": {
    "name": "CREATED_ON"
  },
  "lastModifiedColumn": {
    "name": "LAST_MODIFIED"
  },
  "versionColumn": {
    "name": "VERSION",
    "method": "UUID"
  }
}
2.3 OCI Object Storage Setup
Steps:

Create Object Storage Bucket
- Bucket Name: smebot-images
- Storage Tier: Standard (for frequently accessed images)
- Encryption: Oracle-managed keys (or customer-managed)
- Visibility: Private
- Emit Object Events: Enabled (optional, for tracking)
Configure Bucket Policies
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowBotUpload",
      "Effect": "Allow",
      "Principal": {
        "Service": "objectstorage.oracle.com"
      },
      "Action": [
        "PutObject",
        "GetObject",
        "DeleteObject"
      ],
      "Resource": "arn:oci:objectstorage:*:smebot-images/*"
    }
  ]
}
Generate Pre-Authenticated Request (PAR) URLs
For public read access to uploaded images
Configure expiration policies
Create API Keys
Generate API signing keys for programmatic access
Store private key securely
Phase 3: Code Migration Implementation (Duration: 3-5 days)
3.1 Dependencies Update
Create requirements.txt:

# Existing dependencies
python-telegram-bot==13.7
python-dotenv==0.19.0

# Remove these:
# faunadb
# cloudinary

# Add Oracle dependencies:
oracledb==1.4.0  # Oracle Python driver
oci==2.119.0     # Oracle Cloud Infrastructure SDK
cx_Oracle==8.3.0 # Alternative Oracle driver
Install command:

pip install -r requirements.txt
3.2 Create Oracle Database Abstraction Layer
Create new file: oracle_db.py

"""
Oracle Autonomous JSON Database client wrapper
Provides SODA (Simple Oracle Document Access) operations
"""

import oracledb
import json
import os
from typing import Dict, Any, Optional, List
from dotenv import load_dotenv

load_dotenv()

class OracleJSONClient:
    """Client for Oracle Autonomous JSON Database using SODA"""
    
    def __init__(self):
        self.username = os.getenv('ORACLE_DB_USER', 'ADMIN')
        self.password = os.getenv('ORACLE_DB_PASSWORD')
        self.dsn = os.getenv('ORACLE_DB_DSN')
        self.config_dir = os.getenv('ORACLE_WALLET_DIR', './wallet')
        self.wallet_password = os.getenv('ORACLE_WALLET_PASSWORD')
        
        # Initialize connection pool
        self.pool = None
        self._init_pool()
    
    def _init_pool(self):
        """Initialize Oracle connection pool"""
        try:
            # Configure Oracle client for cloud wallet
            oracledb.init_oracle_client(config_dir=self.config_dir)
            
            # Create connection pool
            self.pool = oracledb.create_pool(
                user=self.username,
                password=self.password,
                dsn=self.dsn,
                min=2,
                max=10,
                increment=1,
                threaded=True,
                wallet_password=self.wallet_password
            )
            print("✓ Oracle connection pool initialized successfully")
        except Exception as e:
            print(f"✗ Error initializing Oracle connection: {e}")
            raise
    
    def get_connection(self):
        """Get a connection from the pool"""
        return self.pool.acquire()
    
    def get_collection(self, collection_name: str):
        """Get or create a SODA collection"""
        conn = self.get_connection()
        try:
            soda = conn.getSodaDatabase()
            collection = soda.openCollection(collection_name)
            
            if collection is None:
                # Create collection if it doesn't exist
                collection = soda.createCollection(collection_name)
                print(f"✓ Created collection: {collection_name}")
            
            return collection, conn
        except Exception as e:
            conn.close()
            raise e
    
    def create_document(self, collection_name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create a new document in a collection
        Equivalent to FaunaDB: q.create(q.collection('User'), {"data": {...}})
        """
        collection, conn = None, None
        try:
            collection, conn = self.get_collection(collection_name)
            
            # Insert document
            doc = collection.insertOneAndGet(data)
            
            # Commit transaction
            conn.commit()
            
            # Return document with key
            result = {
                "ref": {"id": doc.key},
                "data": data,
                "key": doc.key
            }
            
            print(f"✓ Document created with ID: {doc.key}")
            return result
            
        except Exception as e:
            if conn:
                conn.rollback()
            print(f"✗ Error creating document: {e}")
            raise
        finally:
            if conn:
                conn.close()
    
    def update_document(self, collection_name: str, doc_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update an existing document
        Equivalent to FaunaDB: q.update(q.ref(q.collection("User"), id), {"data": {...}})
        """
        collection, conn = None, None
        try:
            collection, conn = self.get_collection(collection_name)
            
            # Get existing document
            doc = collection.find().key(doc_id).getOne()
            
            if doc is None:
                raise ValueError(f"Document with ID {doc_id} not found")
            
            # Get current content
            current_content = doc.getContent()
            
            # Merge updates
            current_content.update(data)
            
            # Replace document
            new_doc = {"key": doc_id, "value": current_content}
            collection.find().key(doc_id).replaceOne(new_doc)
            
            # Commit transaction
            conn.commit()
            
            print(f"✓ Document updated: {doc_id}")
            return {"ref": {"id": doc_id}, "data": current_content}
            
        except Exception as e:
            if conn:
                conn.rollback()
            print(f"✗ Error updating document: {e}")
            raise
        finally:
            if conn:
                conn.close()
    
    def get_document(self, collection_name: str, doc_id: str) -> Optional[Dict[str, Any]]:
        """Get a document by ID"""
        collection, conn = None, None
        try:
            collection, conn = self.get_collection(collection_name)
            
            doc = collection.find().key(doc_id).getOne()
            
            if doc is None:
                return None
            
            return {
                "ref": {"id": doc.key},
                "data": doc.getContent()
            }
            
        finally:
            if conn:
                conn.close()
    
    def find_documents(self, collection_name: str, filter_spec: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Find documents matching a filter
        filter_spec example: {"chat_id": 12345}
        """
        collection, conn = None, None
        try:
            collection, conn = self.get_collection(collection_name)
            
            # Create QBE (Query By Example) filter
            docs = collection.find().filter(filter_spec).getDocuments()
            
            results = []
            for doc in docs:
                results.append({
                    "ref": {"id": doc.key},
                    "data": doc.getContent()
                })
            
            return results
            
        finally:
            if conn:
                conn.close()
    
    def delete_document(self, collection_name: str, doc_id: str) -> bool:
        """Delete a document by ID"""
        collection, conn = None, None
        try:
            collection, conn = self.get_collection(collection_name)
            
            result = collection.find().key(doc_id).remove()
            conn.commit()
            
            return result > 0
            
        except Exception as e:
            if conn:
                conn.rollback()
            print(f"✗ Error deleting document: {e}")
            raise
        finally:
            if conn:
                conn.close()
    
    def close(self):
        """Close connection pool"""
        if self.pool:
            self.pool.close()
            print("✓ Oracle connection pool closed")


# Create singleton instance
oracle_client = OracleJSONClient()
3.3 Create Oracle Object Storage Abstraction Layer
Create new file: oracle_storage.py

"""
Oracle Cloud Infrastructure Object Storage client wrapper
Replaces Cloudinary functionality
"""

import oci
import os
import io
import uuid
from typing import Optional, Dict, Any
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv()

class OracleObjectStorage:
    """Client for OCI Object Storage"""
    
    def __init__(self):
        self.config = {
            "user": os.getenv('OCI_USER_OCID'),
            "key_file": os.getenv('OCI_KEY_FILE_PATH', './oci_api_key.pem'),
            "fingerprint": os.getenv('OCI_FINGERPRINT'),
            "tenancy": os.getenv('OCI_TENANCY_OCID'),
            "region": os.getenv('OCI_REGION', 'us-ashburn-1')
        }
        
        self.namespace = os.getenv('OCI_NAMESPACE')
        self.bucket_name = os.getenv('OCI_BUCKET_NAME', 'smebot-images')
        
        # Initialize OCI client
        self.client = oci.object_storage.ObjectStorageClient(self.config)
        print("✓ OCI Object Storage client initialized")
    
    def upload(self, file_data, filename: Optional[str] = None, 
               folder: str = "uploads", **options) -> Dict[str, Any]:
        """
        Upload a file to Object Storage
        Equivalent to Cloudinary: upload(file_data, options)
        
        Args:
            file_data: File data (bytes, file object, or file path)
            filename: Optional custom filename
            folder: Folder/prefix in bucket
            **options: Additional options (metadata, content_type, etc.)
        
        Returns:
            Dict with upload results including public URL
        """
        try:
            # Generate unique filename if not provided
            if filename is None:
                filename = f"{uuid.uuid4()}.jpg"
            
            # Create object name with folder prefix
            object_name = f"{folder}/{filename}"
            
            # Handle different input types
            if isinstance(file_data, str):
                # File path
                with open(file_data, 'rb') as f:
                    file_content = f.read()
            elif isinstance(file_data, bytes):
                file_content = file_data
            elif hasattr(file_data, 'read'):
                # File-like object
                file_content = file_data.read()
            else:
                raise ValueError("Invalid file_data type")
            
            # Determine content type
            content_type = options.get('content_type', 'image/jpeg')
            
            # Prepare metadata
            metadata = options.get('metadata', {})
            metadata['uploaded_at'] = datetime.utcnow().isoformat()
            
            # Upload to Object Storage
            response = self.client.put_object(
                namespace_name=self.namespace,
                bucket_name=self.bucket_name,
                object_name=object_name,
                put_object_body=file_content,
                content_type=content_type,
                metadata=metadata
            )
            
            # Generate public URL or PAR
            public_url = self._generate_public_url(object_name)
            
            result = {
                "public_id": object_name,
                "version": response.headers.get('ETag', ''),
                "signature": response.headers.get('opc-request-id', ''),
                "width": options.get('width'),
                "height": options.get('height'),
                "format": filename.split('.')[-1] if '.' in filename else 'jpg',
                "resource_type": "image",
                "created_at": datetime.utcnow().isoformat(),
                "bytes": len(file_content),
                "type": "upload",
                "url": public_url,
                "secure_url": public_url,
                "object_name": object_name,
                "namespace": self.namespace,
                "bucket": self.bucket_name
            }
            
            print(f"✓ File uploaded successfully: {object_name}")
            return result
            
        except Exception as e:
            print(f"✗ Error uploading file: {e}")
            raise
    
    def _generate_public_url(self, object_name: str, expiration_hours: int = 24) -> str:
        """
        Generate a Pre-Authenticated Request (PAR) URL for public access
        
        Args:
            object_name: Name of the object in the bucket
            expiration_hours: Hours until URL expires (default 24)
        
        Returns:
            Public URL string
        """
        try:
            # Calculate expiration time
            expiration_time = datetime.utcnow() + timedelta(hours=expiration_hours)
            
            # Create PAR request
            par_details = oci.object_storage.models.CreatePreauthenticatedRequestDetails(
                name=f"par-{uuid.uuid4().hex[:8]}",
                object_name=object_name,
                access_type="ObjectRead",
                time_expires=expiration_time
            )
            
            # Create PAR
            par_response = self.client.create_preauthenticated_request(
                namespace_name=self.namespace,
                bucket_name=self.bucket_name,
                create_preauthenticated_request_details=par_details
            )
            
            # Construct full URL
            base_url = f"https://objectstorage.{self.config['region']}.oraclecloud.com"
            par_url = f"{base_url}_{par_response.data.access_uri}"
            
            return par_url
            
        except Exception as e:
            print(f"✗ Error generating public URL: {e}")
            # Fallback to standard URL (requires authentication)
            return f"https://objectstorage.{self.config['region']}.oraclecloud.com/n/{self.namespace}/b/{self.bucket_name}/o/{object_name}"
    
    def delete(self, object_name: str) -> bool:
        """
        Delete an object from Object Storage
        
        Args:
            object_name: Name of the object to delete
        
        Returns:
            True if successful
        """
        try:
            self.client.delete_object(
                namespace_name=self.namespace,
                bucket_name=self.bucket_name,
                object_name=object_name
            )
            print(f"✓ Object deleted: {object_name}")
            return True
            
        except Exception as e:
            print(f"✗ Error deleting object: {e}")
            raise
    
    def get_object(self, object_name: str) -> bytes:
        """
        Download an object from Object Storage
        
        Args:
            object_name: Name of the object to retrieve
        
        Returns:
            Object content as bytes
        """
        try:
            response = self.client.get_object(
                namespace_name=self.namespace,
                bucket_name=self.bucket_name,
                object_name=object_name
            )
            
            return response.data.content
            
        except Exception as e:
            print(f"✗ Error getting object: {e}")
            raise
    
    def list_objects(self, prefix: str = "", limit: int = 100) -> list:
        """
        List objects in the bucket
        
        Args:
            prefix: Filter by prefix (folder)
            limit: Maximum number of objects to return
        
        Returns:
            List of object names
        """
        try:
            response = self.client.list_objects(
                namespace_name=self.namespace,
                bucket_name=self.bucket_name,
                prefix=prefix,
                limit=limit
            )
            
            return [obj.name for obj in response.data.objects]
            
        except Exception as e:
            print(f"✗ Error listing objects: {e}")
            raise


# Create singleton instance
oracle_storage = OracleObjectStorage()
3.4 Update Configuration File
Update config.py:

"""
Updated configuration with Oracle Cloud credentials
"""

import os
from dotenv import load_dotenv

load_dotenv()

# Telegram Bot Token
TOKEN = os.getenv('BOT_TOKEN')

# Oracle Autonomous JSON Database Configuration
ORACLE_DB_USER = os.getenv('ORACLE_DB_USER', 'ADMIN')
ORACLE_DB_PASSWORD = os.getenv('ORACLE_DB_PASSWORD')
ORACLE_DB_DSN = os.getenv('ORACLE_DB_DSN')
ORACLE_WALLET_DIR = os.getenv('ORACLE_WALLET_DIR', './wallet')
ORACLE_WALLET_PASSWORD = os.getenv('ORACLE_WALLET_PASSWORD')

# OCI Object Storage Configuration
OCI_USER_OCID = os.getenv('OCI_USER_OCID')
OCI_KEY_FILE_PATH = os.getenv('OCI_KEY_FILE_PATH', './oci_api_key.pem')
OCI_FINGERPRINT = os.getenv('OCI_FINGERPRINT')
OCI_TENANCY_OCID = os.getenv('OCI_TENANCY_OCID')
OCI_REGION = os.getenv('OCI_REGION', 'us-ashburn-1')
OCI_NAMESPACE = os.getenv('OCI_NAMESPACE')
OCI_BUCKET_NAME = os.getenv('OCI_BUCKET_NAME', 'smebot-images')

# Legacy credentials (for reference during migration)
# api_secret = os.getenv('API_SECRET')
# api_key = os.getenv('API_KEY')
# FAUNA_KEY = os.getenv('FAUNA_KEY')
3.5 Update Handlers File
Update handlers.py:

"""
Updated handlers with Oracle Cloud integration
"""

from telegram import (
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove, Update,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    CommandHandler, CallbackContext,
    ConversationHandler, MessageHandler,
    Filters, Updater, CallbackQueryHandler
)

# Import Oracle clients instead of FaunaDB and Cloudinary
from oracle_db import oracle_client
from oracle_storage import oracle_storage

# Define States
CHOOSING, CLASS_STATE, SME_DETAILS, CHOOSE_PREF, \
    SME_CAT, ADD_PRODUCTS, SHOW_STOCKS, POST_VIEW_PRODUCTS = range(8)


def start(update, context: CallbackContext) -> int:
    """Start command handler"""
    print("Bot started by user")
    bot = context.bot
    chat_id = update.message.chat.id
    
    bot.send_message(
        chat_id=chat_id,
        text="Hi fellow, Welcome to SMEbot, "
        "Please tell me about yourself, "
        "provide your full name, email, and phone number, "
        "separated by comma each e.g: "
        "John Doe, JohnD@gmail.com, +234567897809"
    )
    return CHOOSING


def choose(update, context):
    """
    Get user data and store in Oracle Autonomous JSON Database
    Migrated from FaunaDB
    """
    bot = context.bot
    chat_id = update.message.chat.id
    
    # Parse user input
    data = update.message.text.split(',')
    
    if len(data) != 3:
        bot.send_message(
            chat_id=chat_id,
            text="Invalid entry, please make sure to input the details "
            "as requested in the instructions"
        )
        bot.send_message(
            chat_id=chat_id,
            text="Type /start, to restart bot"
        )
        return ConversationHandler.END
    
    # Clean data
    name = data[0].strip()
    email = data[1].strip()
    telephone = data[2].strip()
    
    try:
        # Check if user already exists
        existing_users = oracle_client.find_documents(
            "User",
            {"chat_id": chat_id}
        )
        
        if existing_users:
            bot.send_message(
                chat_id=chat_id,
                text="You are already registered! Use /start to begin."
            )
            user_doc = existing_users[0]
            context.user_data["user-id"] = user_doc["ref"]["id"]
            context.user_data["user-name"] = user_doc["data"]["name"]
            context.user_data['user-data'] = user_doc['data']
        else:
            # Create new user document in Oracle JSON Database
            new_user_data = {
                "name": name,
                "email": email,
                "telephone": telephone,
                "is_smeowner": False,
                "preference": "",
                "chat_id": chat_id
            }
            
            new_user = oracle_client.create_document("User", new_user_data)
            
            # Store user context
            context.user_data["user-id"] = new_user["ref"]["id"]
            context.user_data["user-name"] = name
            context.user_data['user-data'] = new_user['data']
            
            print(f"✓ New user created: {name} (ID: {new_user['ref']['id']})")
        
        # Present user classification options
        reply_keyboard = [
            [
                InlineKeyboardButton(
                    text="SME",
                    callback_data="SME"
                ),
                InlineKeyboardButton(
                    text="Customer",
                    callback_data="Customer"
                )
            ]
        ]
        markup = InlineKeyboardMarkup(reply_keyboard, one_time_keyboard=True)
        
        bot.send_message(
            chat_id=chat_id,
            text="Collected information successfully!..🎉🎉 \n"
            "Which of the following do you identify as?",
            reply_markup=markup
        )
        return CLASS_STATE
        
    except Exception as e:
        print(f"✗ Error in choose handler: {e}")
        bot.send_message(
            chat_id=chat_id,
            text="An error occurred. Please try again later or contact support."
        )
        return ConversationHandler.END


def classer(update, context):
    """
    Handle user classification (SME vs Customer)
    Migrated from FaunaDB
    """
    bot = context.bot
    chat_id = update.callback_query.message.chat.id
    name = context.user_data.get("user-name", "User")
    user_id = context.user_data.get("user-id")
    
    try:
        if update.callback_query.data.lower() == "sme":
            # Update user as SME owner in Oracle JSON Database
            oracle_client.update_document(
                "User",
                user_id,
                {"is_smeowner": True}
            )
            
            bot.send_message(
                chat_id=chat_id,
                text=f"Great! {name}, please tell me about your business, "
                "provide your BrandName, Brand email, Address, and phone number "
                "in that order, each separated by comma(,) e.g: "
                "JDWears, JDWears@gmail.com, 101-Mike Avenue-Ikeja, +234567897809",
                reply_markup=ReplyKeyboardRemove()
            )
            return SME_DETAILS
        
        # Customer classification
        categories = [
            [
                InlineKeyboardButton(
                    text="Clothing/Fashion",
                    callback_data="Clothing/Fashion"
                ),
                InlineKeyboardButton(
                    text="Hardware Accessories",
                    callback_data="Hardware Accessories"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Food/Kitchen Ware",
                    callback_data="Food/Kitchen Ware"
                ),
                InlineKeyboardButton(
                    text="ArtnDesign",
                    callback_data="ArtnDesign"
                )
            ]
        ]
        
        bot.send_message(
            chat_id=chat_id,
            text="Here's a list of categories available. "
            "Choose one that matches your interest:",
            reply_markup=InlineKeyboardMarkup(categories)
        )
        return CHOOSE_PREF
        
    except Exception as e:
        print(f"✗ Error in classer handler: {e}")
        bot.send_message(
            chat_id=chat_id,
            text="An error occurred. Please try again."
        )
        return ConversationHandler.END


def handle_image_upload(update, context):
    """
    Example handler for image uploads
    Migrated from Cloudinary to Oracle Object Storage
    """
    bot = context.bot
    chat_id = update.message.chat.id
    
    try:
        # Get photo from message
        photo = update.message.photo[-1]  # Get highest resolution
        
        # Download photo
        file = bot.get_file(photo.file_id)
        photo_bytes = file.download_as_bytearray()
        
        # Upload to Oracle Object Storage
        upload_result = oracle_storage.upload(
            file_data=bytes(photo_bytes),
            filename=f"{photo.file_id}.jpg",
            folder="user_uploads",
            metadata={
                "user_id": context.user_data.get("user-id"),
                "chat_id": str(chat_id)
            }
        )
        
        # Send confirmation with image URL
        bot.send_message(
            chat_id=chat_id,
            text=f"✓ Image uploaded successfully!\n"
            f"URL: {upload_result['secure_url']}\n"
            f"Size: {upload_result['bytes']} bytes"
        )
        
        # Store image reference in user document (optional)
        user_id = context.user_data.get("user-id")
        if user_id:
            user_doc = oracle_client.get_document("User", user_id)
            images = user_doc['data'].get('images', [])
            images.append({
                "object_name": upload_result['object_name'],
                "url": upload_result['secure_url'],
                "uploaded_at": upload_result['created_at']
            })
            oracle_client.update_document("User", user_id, {"images": images})
        
    except Exception as e:
        print(f"✗ Error uploading image: {e}")
        bot.send_message(
            chat_id=chat_id,
            text="Failed to upload image. Please try again."
        )


def cancel(update: Update, context: CallbackContext) -> int:
    """Cancel conversation"""
    update.message.reply_text(
        'Bye! I hope we can talk again some day.',
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END
3.6 Update Main Application File
Update main.py:

"""
Main application entry point with Oracle Cloud integration
"""

import handlers
from telegram.ext import (
    CommandHandler, CallbackContext,
    ConversationHandler, MessageHandler,
    Filters, Updater, CallbackQueryHandler
)
from config import TOKEN
from oracle_db import oracle_client
from oracle_storage import oracle_storage
import atexit

# Initialize updater
updater = Updater(token=TOKEN, use_context=True)
print("✓ Telegram Bot initialized")
dispatcher = updater.dispatcher


def cleanup():
    """Cleanup function for graceful shutdown"""
    print("\n🔄 Shutting down gracefully...")
    oracle_client.close()
    print("✓ Shutdown complete")


# Register cleanup function
atexit.register(cleanup)


def main():
    """Main application entry point"""
    try:
        # Configure conversation handler
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler('start', handlers.start)],
            states={
                handlers.CHOOSING: [
                    MessageHandler(Filters.all, handlers.choose)
                ],
                handlers.CLASS_STATE: [
                    CallbackQueryHandler(handlers.classer)
                ],
                handlers.SME_DETAILS: [
                    MessageHandler(Filters.text & ~Filters.command, handlers.handle_sme_details)
                ],
                handlers.CHOOSE_PREF: [
                    CallbackQueryHandler(handlers.handle_preference)
                ]
                # Add more states as needed
            },
            fallbacks=[CommandHandler('cancel', handlers.cancel)],
            allow_reentry=True
        )
        
        # Add handlers
        dispatcher.add_handler(conv_handler)
        
        # Add image upload handler
        dispatcher.add_handler(
            MessageHandler(Filters.photo, handlers.handle_image_upload)
        )
        
        print("✓ Bot handlers configured")
        print("🚀 Starting bot polling...")
        
        # Start bot
        updater.start_polling()
        updater.idle()
        
    except KeyboardInterrupt:
        print("\n⚠️  Bot stopped by user")
    except Exception as e:
        print(f"✗ Error in main: {e}")
        raise


if __name__ == '__main__':
    main()
3.7 Environment Configuration
Create .env.example file:

# Telegram Bot Configuration
BOT_TOKEN=your_telegram_bot_token

# Oracle Autonomous JSON Database Configuration
ORACLE_DB_USER=ADMIN
ORACLE_DB_PASSWORD=your_oracle_db_password
ORACLE_DB_DSN=your_database_connection_string
ORACLE_WALLET_DIR=./wallet
ORACLE_WALLET_PASSWORD=your_wallet_password

# Oracle Cloud Infrastructure Object Storage Configuration
OCI_USER_OCID=ocid1.user.oc1..your_user_ocid
OCI_KEY_FILE_PATH=./oci_api_key.pem
OCI_FINGERPRINT=your_key_fingerprint
OCI_TENANCY_OCID=ocid1.tenancy.oc1..your_tenancy_ocid
OCI_REGION=us-ashburn-1
OCI_NAMESPACE=your_namespace
OCI_BUCKET_NAME=smebot-images

# Legacy Configuration (for migration reference)
# FAUNA_KEY=your_fauna_key
# API_KEY=your_cloudinary_key
# API_SECRET=your_cloudinary_secret
Phase 4: Testing & Validation (Duration: 2-3 days)
4.1 Unit Testing
Create test_oracle_db.py:

"""
Unit tests for Oracle JSON Database operations
"""

import unittest
from oracle_db import oracle_client


class TestOracleDB(unittest.TestCase):
    
    def setUp(self):
        """Setup test environment"""
        self.test_collection = "TestUser"
        self.test_data = {
            "name": "Test User",
            "email": "test@example.com",
            "telephone": "+1234567890",
            "is_smeowner": False,
            "chat_id": 123456
        }
    
    def test_create_document(self):
        """Test document creation"""
        result = oracle_client.create_document(
            self.test_collection,
            self.test_data
        )
        self.assertIsNotNone(result)
        self.assertIn("ref", result)
        self.assertIn("id", result["ref"])
        
        # Cleanup
        oracle_client.delete_document(
            self.test_collection,
            result["ref"]["id"]
        )
    
    def test_update_document(self):
        """Test document update"""
        # Create document
        doc = oracle_client.create_document(
            self.test_collection,
            self.test_data
        )
        doc_id = doc["ref"]["id"]
        
        # Update document
        update_data = {"is_smeowner": True}
        result = oracle_client.update_document(
            self.test_collection,
            doc_id,
            update_data
        )
        
        self.assertTrue(result["data"]["is_smeowner"])
        
        # Cleanup
        oracle_client.delete_document(self.test_collection, doc_id)
    
    def test_find_documents(self):
        """Test document query"""
        # Create test document
        doc = oracle_client.create_document(
            self.test_collection,
            self.test_data
        )
        doc_id = doc["ref"]["id"]
        
        # Find document
        results = oracle_client.find_documents(
            self.test_collection,
            {"chat_id": 123456}
        )
        
        self.assertGreater(len(results), 0)
        
        # Cleanup
        oracle_client.delete_document(self.test_collection, doc_id)


if __name__ == '__main__':
    unittest.main()
Create test_oracle_storage.py:

"""
Unit tests for Oracle Object Storage operations
"""

import unittest
import io
from oracle_storage import oracle_storage


class TestOracleStorage(unittest.TestCase):
    
    def setUp(self):
        """Setup test environment"""
        self.test_content = b"Test image content"
        self.test_filename = "test_image.jpg"
    
    def test_upload_and_delete(self):
        """Test file upload and deletion"""
        # Upload file
        result = oracle_storage.upload(
            file_data=self.test_content,
            filename=self.test_filename,
            folder="test_uploads"
        )
        
        self.assertIsNotNone(result)
        self.assertIn("secure_url", result)
        self.assertIn("object_name", result)
        
        object_name = result["object_name"]
        
        # Verify upload
        objects = oracle_storage.list_objects(prefix="test_uploads")
        self.assertIn(object_name, objects)
        
        # Delete file
        success = oracle_storage.delete(object_name)
        self.assertTrue(success)
        
        # Verify deletion
        objects_after = oracle_storage.list_objects(prefix="test_uploads")
        self.assertNotIn(object_name, objects_after)
    
    def test_list_objects(self):
        """Test listing objects"""
        objects = oracle_storage.list_objects(limit=10)
        self.assertIsInstance(objects, list)


if __name__ == '__main__':
    unittest.main()
4.2 Integration Testing
Test scenarios:

✅ User registration flow
✅ SME owner classification
✅ Customer preference selection
✅ Image upload and storage
✅ Data retrieval and updates
✅ Error handling and recovery
Phase 5: Data Migration (Duration: 1-2 days)
5.1 Create Migration Script
Create migrate_data.py:

"""
Data migration script from FaunaDB to Oracle Autonomous JSON Database
"""

from faunadb import query as q
from faunadb.client import FaunaClient
from oracle_db import oracle_client
from config import FAUNA_KEY
import os
from dotenv import load_dotenv

load_dotenv()

def migrate_fauna_to_oracle():
    """Migrate all data from FaunaDB to Oracle"""
    
    print("🔄 Starting data migration from FaunaDB to Oracle...")
    
    # Initialize FaunaDB client
    fauna_client = FaunaClient(secret=FAUNA_KEY)
    
    try:
        # Get all users from FaunaDB
        result = fauna_client.query(
            q.map_(
                q.lambda_("ref", q.get(q.var("ref"))),
                q.paginate(q.documents(q.collection("User")), size=1000)
            )
        )
        
        users = result["data"]
        print(f"📊 Found {len(users)} users to migrate")
        
        # Migrate each user
        migrated_count = 0
        failed_count = 0
        
        for user in users:
            try:
                user_data = user["data"]
                
                # Check if user already exists in Oracle (by chat_id)
                existing = oracle_client.find_documents(
                    "User",
                    {"chat_id": user_data.get("chat_id")}
                )
                
                if existing:
                    print(f"⚠️  User with chat_id {user_data.get('chat_id')} already exists, skipping...")
                    continue
                
                # Create user in Oracle
                oracle_client.create_document("User", user_data)
                migrated_count += 1
                print(f"✓ Migrated user: {user_data.get('name', 'Unknown')}")
                
            except Exception as e:
                failed_count += 1
                print(f"✗ Failed to migrate user: {e}")
        
        print(f"\n✅ Migration complete!")
        print(f"   - Successfully migrated: {migrated_count}")
        print(f"   - Failed: {failed_count}")
        print(f"   - Total: {len(users)}")
        
    except Exception as e:
        print(f"✗ Migration failed: {e}")
        raise
    finally:
        oracle_client.close()


if __name__ == "__main__":
    confirm = input("⚠️  This will migrate data from FaunaDB to Oracle. Continue? (yes/no): ")
    if confirm.lower() == "yes":
        migrate_fauna_to_oracle()
    else:
        print("Migration cancelled.")
Run migration:

python migrate_data.py
Phase 6: Deployment & Monitoring (Duration: 1-2 days)
6.1 Deployment Checklist
Oracle Autonomous JSON Database configured and tested
OCI Object Storage bucket created and configured
Wallet files downloaded and placed in project
API keys generated and secured
Environment variables configured
Dependencies installed
All tests passing
Data migration completed
Rollback plan prepared
6.2 Monitoring Setup
Create monitoring script: monitor.py

"""
Monitoring and health check script
"""

from oracle_db import oracle_client
from oracle_storage import oracle_storage
import time

def health_check():
    """Perform health check on Oracle services"""
    print("🏥 Performing health check...")
    
    # Check database connection
    try:
        test_doc = oracle_client.create_document(
            "HealthCheck",
            {"timestamp": time.time(), "status": "healthy"}
        )
        oracle_client.delete_document("HealthCheck", test_doc["ref"]["id"])
        print("✓ Database: Healthy")
    except Exception as e:
        print(f"✗ Database: Unhealthy - {e}")
    
    # Check object storage
    try:
        test_data = b"health check"
        result = oracle_storage.upload(
            file_data=test_data,
            filename="health_check.txt",
            folder="system"
        )
        oracle_storage.delete(result["object_name"])
        print("✓ Object Storage: Healthy")
    except Exception as e:
        print(f"✗ Object Storage: Unhealthy - {e}")


if __name__ == "__main__":
    health_check()
Phase 7: Cost Optimization (Ongoing)
7.1 Oracle Cloud Free Tier Resources
Always Free Resources:

2 Oracle Autonomous Databases (20GB each)
20GB Object Storage
10GB outbound data transfer per month
Cost Optimization Tips:

Use Always Free tier resources for development
Enable auto-scaling for production
Implement object lifecycle policies for old images
Use compression for large files
Monitor usage with OCI Cost Management
Phase 8: Documentation & Training (Duration: 1 day)
8.1 Updated Documentation
Create MIGRATION_GUIDE.md:

# Migration Guide: Fauna/Cloudinary → Oracle Cloud

## Overview
This document describes the migration from FaunaDB and Cloudinary to Oracle Cloud Infrastructure.

## Architecture Changes

### Database: FaunaDB → Oracle Autonomous JSON Database
- **Before**: FaunaDB with collections and documents
- **After**: Oracle Autonomous JSON DB with SODA collections
- **Key Changes**:
  - `client.query(q.create(...))` → `oracle_client.create_document(...)`
  - `client.query(q.update(...))` → `oracle_client.update_document(...)`
  - `client.query(q.get(...))` → `oracle_client.get_document(...)`

### Storage: Cloudinary → OCI Object Storage
- **Before**: Cloudinary with transformation API
- **After**: OCI Object Storage with PAR URLs
- **Key Changes**:
  - `cloudinary.uploader.upload(...)` → `oracle_storage.upload(...)`
  - Public URLs generated via Pre-Authenticated Requests
  - Image transformations moved to OCI Vision API (optional)

## Setup Instructions

### 1. Oracle Cloud Account
```bash
# Sign up at: https://cloud.oracle.com/
# Navigate to Console
# Create compartment for bot project
2. Database Setup
# Download wallet files from Oracle Cloud Console
# Place in ./wallet directory
# Update .env with connection details
3. Object Storage Setup
# Create bucket in OCI Console
# Generate API keys
# Configure .env with credentials
4. Install Dependencies
pip install -r requirements.txt
5. Run Migration
python migrate_data.py
6. Test Application
python test_oracle_db.py
python test_oracle_storage.py
7. Start Bot
python main.py
Troubleshooting
Database Connection Issues
Verify wallet files are in correct location
Check firewall/network access
Validate DSN format
Object Storage Issues
Verify API key fingerprint
Check bucket permissions
Validate region configuration
Rollback Plan
If issues occur, revert to FaunaDB/Cloudinary:

Stop new bot instance
Restore original code from backup
Verify FaunaDB connection
Resume service
Support
For issues, contact: [your-email@example.com]


---

## **Summary of Benefits**

### Oracle Cloud vs. Current Solution:

| Aspect | FaunaDB/Cloudinary | Oracle Cloud | Benefit |
|--------|-------------------|--------------|---------|
| **Cost** | Pay per operation | Free tier + pay-as-you-go | Lower costs for small projects |
| **Scalability** | Auto-scaling | Auto-scaling + manual control | More control |
| **Performance** | Good | Excellent (enterprise-grade) | Better performance |
| **Integration** | Separate services | Unified platform | Easier management |
| **Security** | Good | Enterprise-grade | Enhanced security |
| **Reliability** | 99.9% SLA | 99.95% SLA | Higher uptime |
| **Support** | Community/Paid | 24/7 Enterprise | Better support |
| **Vendor Lock-in** | Moderate | Moderate | Similar |

---

## **Migration Timeline Summary**

| Phase | Duration | Key Activities |
|-------|----------|----------------|
| 1. Analysis | 1-2 days | Architecture design, service selection |
| 2. Setup | 2-3 days | Oracle account, database, storage setup |
| 3. Implementation | 3-5 days | Code migration, new abstractions |
| 4. Testing | 2-3 days | Unit tests, integration tests |
| 5. Data Migration | 1-2 days | Migrate existing data |
| 6. Deployment | 1-2 days | Deploy and monitor |
| 7. Optimization | Ongoing | Cost optimization, performance tuning |
| **Total** | **10-17 days** | **Complete migration** |

---

## **Next Steps**

1. **Review this plan** with your team
2. **Set up Oracle Cloud account** and allocate budget
3. **Create development environment** for testing
4. **Begin Phase 1** (Analysis & Architecture)
5. **Schedule regular reviews** during migration
6. **Plan for rollback** scenario if needed