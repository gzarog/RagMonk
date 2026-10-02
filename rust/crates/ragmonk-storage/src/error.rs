use ragmonk_core::errors::{ErrorKind, RagMonkError};

#[derive(Debug, thiserror::Error)]
pub enum StorageError {
    #[error("database error ({context}): {source}")]
    Sqlite {
        context: String,
        #[source]
        source: rusqlite::Error,
    },
    #[error("{0}")]
    Io(String),
    /// The database was written by a newer RagMonk; refuse to touch it.
    #[error("{db} has schema version {found}, newer than this build supports ({supported}); upgrade RagMonk")]
    SchemaTooNew {
        db: String,
        found: i64,
        supported: i64,
    },
    #[error("{0}")]
    Invalid(String),
    #[error("not found: {0}")]
    NotFound(String),
}

impl StorageError {
    pub fn sqlite(context: impl Into<String>) -> impl FnOnce(rusqlite::Error) -> Self {
        let context = context.into();
        move |source| StorageError::Sqlite { context, source }
    }
}

impl From<StorageError> for RagMonkError {
    fn from(e: StorageError) -> Self {
        let kind = match e {
            StorageError::NotFound(_) | StorageError::Invalid(_) => ErrorKind::Usage,
            _ => ErrorKind::Database,
        };
        RagMonkError::new(kind, e.to_string())
    }
}

pub type Result<T> = std::result::Result<T, StorageError>;
