# Example Video API (synthetic test page)

Invented text and prices, shaped like a provider's Markdown docs: a YAML
description block with indented tables and blank lines between rows.

      description: >-
        Some introduction that mentions a | pipe | outside any table.

        # Pricing

        Price per second (USD)

        | Model | 480p | 720 p| 1080p |

        | --- | --- | --- | --- |

        | example-video | $0.10 | $0.20 | $0.50 |

        | example-video-fast | $0.05 | $0.10 | |

        | example-video-legacy | $0.01 | $0.02 | $0.03 |

        Fast does not support 1080p.

        With an input video, a different table applies:

        | Version | 720p | 1080p |

        | --- | --- | --- |

        | 1.0 | 0.08 | N/A |

        | 0.9 | 0.05 | 0.07 |
