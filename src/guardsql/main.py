from guardsql.api.app import create_app
from guardsql.config import get_settings

app = create_app(cors_origins=get_settings().cors_origin_list)
