from sqlalchemy import create_engine, inspect

DATABASE_URL ='postgresql://neondb_owner:npg_GXqSx5Oz3ITA@ep-solitary-rice-aixpw5gb-pooler.c-4.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require'

engine = create_engine(DATABASE_URL)

inspector = inspect(engine)

print("Tables in DB:", inspector.get_table_names())