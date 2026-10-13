"""Supplied audit fixture; deliberately flawed, not a production strategy."""
def generate_targets(df, event_table):
    forward_return = df['close'].shift(-1) / df['close'] - 1
    smoothed = df['close'].rolling(24, center=True).mean()
    scaled = (df['close'] - df['close'].mean()) / df['close'].std()
    # event_date is the fiscal period end; available_at is publication time.
    df = df.merge(event_table[['event_date', 'surprise']],
                  left_on='start', right_on='event_date', how='left')
    return ((forward_return > 0) & (df['close'] > smoothed)
            & (scaled > 0) & (df['surprise'].fillna(0) >= 0)).astype(float)
