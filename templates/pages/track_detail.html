{% extends "base.html" %}

{% block title %}{{ artist_name }} · Popularr{% endblock %}

{% block content %}
<style>
/* Critical artist-page table colors to prevent white flash before late styles load. */
.artist-page .card,
.artist-page .table-responsive,
.artist-page .table,
.artist-page .table tbody,
.artist-page .table tbody tr,
.artist-page .table tbody td {
  background-color: var(--secondary-bg, #1e1e1e) !important;
  color: var(--text-primary, #e8e8e8) !important;
}

.artist-page .table thead,
.artist-page .table thead th {
  background-color: var(--primary-bg, #141414) !important;
  color: var(--text-primary, #e8e8e8) !important;
}

@media (max-width: 576px) {
  /* Artist image: shrink slightly, stay centered */
  .artist-img-wrap {
    width: 150px !important;
    height: 150px !important;
  }

  /* Action button row: ensure adequate touch targets */
  .d-flex.flex-row.flex-wrap .btn {
    min-height: 44px;
  }

  /* Country/IDs btn-group: adequate tap size */
  .btn-group.btn-group-sm .btn {
    min-height: 40px;
    padding: 0.375rem 0.75rem;
  }

  /* Artist ID inputs: prevent iOS zoom */
  .col-12.col-lg-6 input.form-control {
    font-size: 16px;
  }
}
</style>
<div class="container-fluid mt-4 artist-page">
  <nav aria-label="breadcrumb">
    <ol class="breadcrumb">
      <li class="breadcrumb-item"><a href="{{ url_for('dashboard') }}">Dashboard</a></li>
      <li class="breadcrumb-item"><a href="{{ url_for('artists') }}">Artists</a></li>
      <li class="breadcrumb-item active" aria-current="page">{{ artist_name }}</li>
    </ol>
  </nav>

  <div class="d-flex flex-column flex-md-row justify-content-between align-items-start mb-4 gap-3">
    <div class="flex-grow-1">
      <h1 class="h2 mb-2" data-artist-name="{{ artist_name|e }}">
        <i class="bi bi-person-music"></i> {{ artist_name }}
      </h1>
      <p class="text-muted mb-0">Artist overview</p>
    </div>
    <div class="d-flex gap-2 flex-row flex-wrap">
      {% if (qbit_config.enabled and qbit_config.web_url) or slskd_config.enabled %}
      <button class="btn btn-outline-info" onclick='openDownloadSearch({{ artist_name|tojson }})' title="Download {{ artist_name }} releases">
        <i class="bi bi-download"></i> <span class="d-none d-sm-inline">Download</span>
      </button>
      {% endif %}
      <button id="artistFavouriteBtn" class="btn btn-outline-danger" onclick='toggleArtistFavourite({{ artist_name|tojson }})' title="Favourite this artist — new albums will be auto-queued for download" aria-label="Favourite this artist to auto-queue new albums for download">
        <i class="bi bi-heart" id="artistFavouriteIcon"></i> <span class="d-none d-sm-inline" id="artistFavouriteLabel">Favourite</span>
      </button>
      <a class="btn btn-outline-secondary" href="{{ url_for('artists') }}">
        <i class="bi bi-arrow-left"></i> <span class="d-none d-sm-inline">Back</span>
      </a>
      <form action="{{ url_for('scan_artist_custom') }}" method="post" class="d-flex align-items-center gap-2 flex-wrap">
        <input type="hidden" name="artist" value="{{ artist_name }}">
        <select name="scan_type" class="form-select form-select-sm" style="min-width: 160px; width: auto;" title="Select scan type">
          <option value="full">Full</option>
          <option value="navidrome">Navidrome</option>
          <option value="popularity">Popularity</option>
          <option value="metadata">Metadata</option>
          <option value="singles">Singles</option>
          <option value="essentia">Essentia</option>
        </select>
        <div class="form-check form-check-inline mb-0" title="Force full rescan for this artist">
          <input class="form-check-input" type="checkbox" id="artistForceScan" name="force" value="1">
          <label class="form-check-label small" for="artistForceScan">Force</label>
        </div>
        <button type="submit" class="btn btn-warning" title="Run selected scan for this artist">
          <i class="bi bi-play-circle"></i> <span class="d-none d-sm-inline">Scan Artist</span>
        </button>
      </form>
    </div>
  </div>

  <!-- Needs Correcting badge — always shown when corrections exist -->
  <div id="artist-corrections-banner" style="display:none;" class="mb-3">
    <a href="{{ url_for('artist_corrections', name=artist_name) }}" class="btn btn-outline-danger btn-sm">
      <i class="bi bi-tools"></i> Needs Correcting
    </a>
    <span id="artist-missing-tracks-badge" class="badge bg-warning text-dark ms-2" style="display:none;"></span>
  </div>
  <div class="mb-3">
    <a href="{{ url_for('artist_genre_management', name=artist_name) }}" class="btn btn-outline-info btn-sm">
      <i class="bi bi-tags"></i> Genre Management
    </a>
  </div>

  <div class="card mb-4 sticky-top" style="top: 4rem; z-index: 1000;">
    <div class="card-body py-2 px-3">
      <ul class="nav nav-pills gap-2 small">
        <li class="nav-item"><a class="nav-link" href="#artist-overview-section">Overview</a></li>
        <li class="nav-item"><a class="nav-link" href="#artist-stats-section">Stats</a></li>
        <li class="nav-item"><a class="nav-link" href="#artist-similar-section">Similar</a></li>
        <li class="nav-item"><a class="nav-link" href="#artist-top-tracks-section">Top Tracks</a></li>
        <li class="nav-item"><a class="nav-link" href="#artist-genres-section">Genres and Moods</a></li>
        <li class="nav-item"><a class="nav-link" href="#albums-section">Albums</a></li>
        <li class="nav-item"><a class="nav-link" href="#artist-covers-section">Covers</a></li>
        <li class="nav-item"><a class="nav-link" href="#artist-appears-on-section">Appears On</a></li>
      </ul>
    </div>
  </div>

  <!-- Artist Image Section -->
  <div class="mb-4" id="artist-overview-section">
    <div class="card">
      <div class="card-body">
        <div class="row g-4">
          <!-- Artist Image -->
          <div class="col-12 col-md-3 d-flex flex-column align-items-center">
            <div class="artist-img-wrap position-relative mb-3" style="width: 200px; height: 200px; border-radius: 0.5rem; overflow: hidden; background-color: #2a2a2a; border: 2px solid var(--border-color);">
              <img id="artistImage" src="{{ url_for('api_artist_image', name=artist_name) }}" 
                   alt="{{ artist_name }}" 
                   style="width: 100%; height: 100%; object-fit: cover;"
                   onerror="this.src='data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%22200%22 height=%22200%22%3E%3Crect fill=%22%232a2a2a%22 width=%22200%22 height=%22200%22/%3E%3Ctext x=%2250%25%22 y=%2250%25%22 text-anchor=%22middle%22 dy=%22.3em%22 fill=%22%23666%22 font-size=%2216%22%3ENo Image%3C/text%3E%3C/svg%3E';">
              <button class="btn btn-sm btn-dark position-absolute bottom-0 start-50 translate-middle-x mb-2" onclick='openArtistImageModal({{ artist_name|tojson }})' title="Change artist image">
                <i class="bi bi-pencil"></i> Change Image
              </button>
            </div>
          </div>
          <!-- Artist Bio and Country/Origin -->
          <div class="col-12 col-md-9">
            <h5 class="mb-3"><i class="bi bi-info-circle"></i> Artist Bio</h5>
            <div id="artistBio" class="text-muted" style="min-height: 120px;" data-has-initial-bio="{{ '1' if artist_bio else '0' }}">
              {% if artist_bio %}
                <p>{{ artist_bio|e|replace('&lt;br /&gt;', '<br>')|replace('&lt;br/&gt;', '<br>')|replace('&lt;br&gt;', '<br>')|replace('\n', '<br>')|safe }}</p>
              {% else %}
                <p class="text-center mt-2">
                  <small>Loading artist information from database...</small>
                </p>
              {% endif %}
            </div>
            
            <!-- Country/Origin under Bio -->
            <div class="mt-4 pt-3" style="border-top: 1px solid var(--border-color);">
              <h6 class="mb-3"><i class="bi bi-globe"></i> Country/Origin</h6>
              <div id="artistCountryDisplay">
                {% if artist_country %}
                  <div class="d-flex align-items-center gap-2 mb-2">
                    <span class="badge bg-info" style="font-size: 0.95rem; padding: 0.4rem 0.8rem;">
                      {{ artist_country }}
                    </span>
                  </div>
                {% else %}
                  <span class="text-muted small">No country information available.</span>
                {% endif %}
              </div>
              <div class="btn-group btn-group-sm mt-2">
                <button class="btn btn-outline-primary btn-sm" onclick="fetchArtistCountry()" id="fetchArtistCountryBtn">
                  <i class="bi bi-cloud-download"></i> Get from MusicBrainz
                </button>
                <button class="btn btn-outline-secondary btn-sm" onclick="editArtistCountry()" id="editArtistCountryBtn">
                  <i class="bi bi-pencil"></i> Edit Manually
                </button>
              </div>
            </div>

            <div class="mt-4 pt-3" style="border-top: 1px solid var(--border-color);">
              <h6 class="mb-3"><i class="bi bi-people"></i> Band Members</h6>
              <div id="artistMembersDisplay">
                {% if artist_members %}
                  <div class="d-flex flex-wrap gap-2">
                    {% for member in artist_members %}
                      <span class="badge bg-secondary text-light" title="{{ member.relation_type or 'member' }}{% if member.begin %} · since {{ member.begin }}{% endif %}{% if member.end %} · until {{ member.end }}{% endif %}{% if member.attributes %} · {{ member.attributes|join(', ') }}{% endif %}">
                        {{ member.name }}{% if member.ended %} · former{% endif %}
                      </span>
                    {% endfor %}
                  </div>
                {% else %}
                  <span class="text-muted small">No MusicBrainz band members available.</span>
                {% endif %}
              </div>
            </div>

            <!-- Artist IDs below Biography -->
            <div class="mt-4 pt-3" style="border-top: 1px solid var(--border-color);">
              <div class="d-flex justify-content-between align-items-center mb-3">
                <h6 class="mb-0"><i class="bi bi-link-45deg"></i> Artist IDs</h6>
                <button class="btn btn-sm btn-primary" onclick="openEditArtistIdsModal()">
                  <i class="bi bi-pencil"></i> Edit
                </button>
              </div>
              <div class="row g-2">
                <div class="col-12 col-lg-6">
                  <label class="form-label fw-bold small">MusicBrainz Artist ID</label>
                  <div class="input-group input-group-sm">
                    <input type="text" class="form-control" id="musicbrainzArtistId" value="{{ stats.musicbrainz_artist_id or stats.lastfm_artist_mbid or '' }}" readonly>
                    {% if stats.musicbrainz_artist_id or stats.lastfm_artist_mbid %}
                    <a href="https://musicbrainz.org/artist/{{ stats.musicbrainz_artist_id or stats.lastfm_artist_mbid }}" target="_blank" class="btn btn-outline-secondary">
                      <i class="bi bi-box-arrow-up-right"></i>
                    </a>
                    {% endif %}
                  </div>
                </div>
                <div class="col-12">
                  <label class="form-label fw-bold small">Discogs Artist ID</label>
                  <div class="input-group input-group-sm">
                    <input type="text" class="form-control" id="discogsArtistId" value="{{ stats.discogs_artist_id or '' }}" readonly>
                    {% if stats.discogs_artist_id %}
                    <a href="https://www.discogs.com/artist/{{ stats.discogs_artist_id }}" target="_blank" class="btn btn-outline-secondary">
                      <i class="bi bi-box-arrow-up-right"></i>
                    </a>
                    {% endif %}
                  </div>
                </div>
              </div>
              <small class="text-muted d-block mt-2">
                <i class="bi bi-info-circle"></i> Used for API lookups and can be manually edited to correct matching issues.
              </small>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Statistics Cards -->
  <div class="row g-2 g-md-3 mb-4" id="artist-stats-section">
    <div class="col-6 col-md-3">
      <div class="card h-100">
        <div class="card-body text-center">
          <div class="text-muted mb-2" style="font-size: 0.85rem;">
            <i class="bi bi-disc"></i> Albums
          </div>
          <div class="h4 mb-0">{{ stats.album_count or 0 }}</div>
        </div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card h-100">
        <div class="card-body text-center">
          <div class="text-muted mb-2" style="font-size: 0.85rem;">
            <i class="bi bi-music-note"></i> Tracks
          </div>
          <div class="h4 mb-0">{{ stats.track_count or 0 }}</div>
        </div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card h-100">
        <div class="card-body text-center">
          <div class="text-muted mb-2" style="font-size: 0.85rem;">
            <i class="bi bi-star-fill"></i> Avg
          </div>
          <div class="h4 mb-0">{{ stats.avg_stars|round(2) if stats.avg_stars else "—" }}</div>
        </div>
      </div>
    </div>
    <div class="col-6 col-md-3">
      <div class="card h-100">
        <div class="card-body text-center">
          <div class="text-muted mb-2" style="font-size: 0.85rem;">
            <i class="bi bi-star-fill"></i> 5★
          </div>
          <div class="h4 mb-0">{{ stats.five_star_count or 0 }}</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Similar Artists Section -->
  <div class="card mb-4" id="artist-similar-section">
    <div class="card-header">
      <h5 class="card-title mb-0">
        <i class="bi bi-person-heart"></i> Similar Artists
      </h5>
    </div>
    <div class="card-body" style="background-color: var(--secondary-bg);">
      <div id="artistSimilarArtistsContainer">
        <div class="d-flex justify-content-center py-3">
          <div class="spinner-border spinner-border-sm" role="status">
            <span class="visually-hidden">Loading similar artists...</span>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Genre Display and Management -->
  <div class="mb-4" id="artist-genres-section">
    <div class="card">
      <div class="card-header d-flex justify-content-between align-items-center">
        <h5 class="card-title mb-0">
          <i class="bi bi-collection"></i> Genres and Moods
        </h5>
        <button class="btn btn-sm btn-outline-primary" onclick="fetchArtistGenreRecommendations()" id="fetchArtistGenresBtn">
          <i class="bi bi-cloud-download"></i> Get Online Suggestions
        </button>
      </div>
      <div class="card-body">
        <!-- Detected Genres from Database (pre-rendered from scan data) -->
        <div class="mb-4">
          <h6 class="mb-3"><i class="bi bi-database-fill"></i> Detected Genres</h6>
          <p class="text-muted small mb-2">All genres detected from external sources (Last.fm, Discogs, MusicBrainz, Essentia, etc.) across all tracks. These are kept in the database for reference.</p>
          <div id="artistGenreSourcesContainer" class="text-muted small">
            {% set sources = [
              ('mood', 'Mood Scan', 'F59C00', 'light'),
              ('spotify_genres', 'Spotify', '1DB954', 'white'),
              ('lastfm_tags', 'Last.fm', 'D51007', 'black'),
              ('listenbrainz_genres', 'ListenBrainz', '0066FF', 'white'),
              ('discogs_genres', 'Discogs', '6c757d', 'white'),
              ('essentia_genres', 'Essentia', '6f42c1', 'white'),
              ('navidrome_genres', 'Navidrome', '17A589', 'white'),
              ('musicbrainz_genres', 'MusicBrainz', 'EB743B', 'white')
            ] %}
            {% set ns = namespace(has_any=False) %}
            {% for key, label, bgcolor, textcolor in sources %}
              {% if genre_sources and genre_sources.get(key) %}{% set ns.has_any = True %}{% endif %}
            {% endfor %}
            {% if ns.has_any %}
              {% set ns2 = namespace(first=True) %}
              <ul class="nav nav-tabs" role="tablist">
                {% for key, label, bgcolor, textcolor in sources %}
                  {% if genre_sources and genre_sources.get(key) %}
                    <li class="nav-item" role="presentation">
                      <button class="nav-link {{ 'active' if ns2.first else '' }}"
                              id="artistGenreTab{{ key }}-tab" data-bs-toggle="tab"
                              data-bs-target="#artistGenreTab{{ key }}-content"
                              type="button" role="tab" style="color: #{{ bgcolor }};">
                        <i class="bi bi-tag"></i> {{ label }}
                        <span class="badge ms-2" style="background-color: #{{ bgcolor }}; color: {{ textcolor }};">{{ genre_sources[key]|length }}</span>
                      </button>
                    </li>
                    {% set ns2.first = False %}
                  {% endif %}
                {% endfor %}
              </ul>
              {% set ns3 = namespace(first=True) %}
              <div class="tab-content mt-3">
                {% for key, label, bgcolor, textcolor in sources %}
                  {% if genre_sources and genre_sources.get(key) %}
                    <div class="tab-pane fade {{ 'active show' if ns3.first else '' }}"
                         id="artistGenreTab{{ key }}-content" role="tabpanel">
                      <p class="text-muted small mb-2">Tags/moods collected from {{ label }} during scan · sorted by frequency</p>
                      <div class="d-flex flex-wrap gap-2 mb-3" id="artistSourceTags_{{ key }}">
                        {% for genre in genre_sources[key]|sort(reverse=True, attribute='count') %}
                          <label class="badge genre-source-selectable" style="background-color: #{{ bgcolor }}; color: {{ textcolor }}; cursor:pointer; user-select:none; font-size:0.85rem;" title="Click to select">
                            <input type="checkbox" class="artist-source-tag-check" value="{{ genre.name }}" style="margin-right:0.3rem;cursor:pointer;">
                            {{ genre.name }}{% if genre.count %} <small class="opacity-75">({{ genre.count }})</small>{% endif %}
                          </label>
                        {% endfor %}
                      </div>
                      <button class="btn btn-sm btn-outline-success artist-apply-source-tags-btn" style="display:none" onclick="applySelectedArtistSourceTags()">
                        <i class="bi bi-check-circle"></i> Save Selected to All Artist Tracks
                      </button>
                    </div>
                    {% set ns3.first = False %}
                  {% endif %}
                {% endfor %}
              </div>
            {% else %}
              <div class="alert alert-info mb-0">
                <i class="bi bi-info-circle"></i> No genre/mood data available yet.
                <br><small>Genres and moods are collected during popularity scans from Spotify, Last.FM, ListenBrainz, Discogs, and MusicBrainz.</small>
                <br><small class="text-muted mt-2">Next scan will populate this data.</small>
              </div>
            {% endif %}
          </div>
        </div>
        
        <hr style="margin: 1.5rem 0; border-color: var(--border-color);">
        
        <!-- Top Genres from All Tracks -->
        <div class="mb-3">
          <h6 class="mb-2"><i class="bi bi-music-note-beamed"></i> Top Genres</h6>
          <p class="text-muted small mb-2">These are the top genres saved to track metadata across all tracks by this artist (up to 3 per track). Click the × to remove incorrect genres.</p>
          <div id="artistGenresContainer">
            {% if genres %}
            <div class="d-flex flex-wrap gap-2 mb-3" id="currentArtistGenres">
              {% for genre in genres %}
              <label class="badge bg-primary" style="font-size: 1rem; cursor: pointer; user-select: none; display: inline-flex; align-items: center; padding: 0.45rem 0.65rem;">
                <input type="checkbox" value="{{ genre }}" style="margin-right: 0.5rem; cursor: pointer;" onchange="toggleGenreCheckbox('currentArtistGenres', 'removeSelectedArtistGenresBtn')">
                {{ genre }}
              </label>
              {% endfor %}
            </div>
            <button class="btn btn-sm btn-warning" id="removeSelectedArtistGenresBtn" style="display: none; margin-bottom: 1rem;" onclick="removeSelectedArtistGenres()">
              <i class="bi bi-trash"></i> Remove Selected Genres
            </button>
            {% else %}
              <span class="text-muted small">No genres found in library</span>
            {% endif %}
          </div>
        </div>
        
        <!-- Recommended Genres Section -->
        <div id="recommendedArtistGenresSection" style="display: none;">
          <h6 class="mb-2"><i class="bi bi-stars"></i> Suggested Genres from MusicBrainz</h6>
          <p class="text-muted small mb-2">Click genres to select, then apply to all tracks by this artist</p>
          <div class="d-flex flex-wrap gap-2 mb-3" id="recommendedArtistGenres">
            <span class="text-muted small">Loading...</span>
          </div>
          <button class="btn btn-sm btn-success" onclick="applySelectedArtistGenres()" id="applyArtistGenresBtn" style="display: none;">
            <i class="bi bi-check-circle"></i> Apply Selected to All Artist Tracks (MP3/FLAC Files)
          </button>
    </div>
  </div>

  <!-- Top Tracks Section -->
  <div class="card mb-4" id="artist-top-tracks-section">
    <div class="card-header d-flex justify-content-between align-items-center">
      <h5 class="card-title mb-0">
        <i class="bi bi-graph-up-arrow"></i> Top Tracks
      </h5>
      <small class="text-muted">Top 10 by z-score</small>
    </div>
    <div class="card-body p-0">
      <div class="table-responsive">
        <table class="table table-hover mb-0">
          <thead>
            <tr>
              <th style="width: 60px;" class="text-center">#</th>
              <th>Track</th>
              <th>Album</th>
              <th style="width: 90px;" class="text-center">Z-Score</th>
              <th style="width: 90px;" class="text-center">Pop</th>
              <th style="width: 90px;" class="text-center">Stars</th>
            </tr>
          </thead>
          <tbody>
            {% if top_tracks and top_tracks|length > 0 %}
              {% for track in top_tracks %}
                {% set track_is_queued = track.file_path and '__queued_for_download__' in track.file_path %}
                <tr{% if track_is_queued %} class="table-secondary opacity-75"{% endif %}>
                  <td class="text-center text-muted">{{ loop.index }}</td>
                  <td>
                    {% if track_is_queued %}
                      <span class="text-muted">{{ track.title }}</span>
                      <span class="badge bg-warning text-dark ms-2" title="This track is currently in the download queue">In Queue</span>
                    {% else %}
                      <a href="{{ url_for('track_detail', artist=artist_name, album=track.album, track_id=track.id) }}" class="text-decoration-none">
                        {{ track.title }}
                      </a>
                    {% endif %}
                    {% if track.is_single %}
                      <span class="badge bg-warning text-dark ms-2">Single</span>
                    {% endif %}
                  </td>
                  <td>
                    <a href="{{ url_for('album_detail', artist=artist_name, album=track.album) }}" class="text-decoration-none">
                      {{ track.album }}
                    </a>
                  </td>
                  <td class="text-center"><span class="badge bg-info">{{ '%.2f'|format(track.artist_z_score or 0) }}</span></td>
                  <td class="text-center">{{ (track.popularity_score or 0)|round(1) }}</td>
                  <td class="text-center">{{ track.stars or 0 }}★</td>
                </tr>
              {% endfor %}
            {% else %}
              <tr>
                <td colspan="6" class="text-center text-muted py-4">
                  <i class="bi bi-info-circle"></i> No tracks available for ranking
                </td>
              </tr>
            {% endif %}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Additional Stats: Duration and Year Range -->
  <div class="row g-2 g-md-3 mb-4">
    <div class="col-6 col-md-4">
      <div class="card h-100">
        <div class="card-body text-center">
          <div class="text-muted mb-2" style="font-size: 0.85rem;">
            <i class="bi bi-hourglass"></i> Total Duration
          </div>
          <div class="h5 mb-0">
            {% if stats.total_duration %}
              {% set hours = (stats.total_duration / 3600)|int %}
              {% set minutes = ((stats.total_duration % 3600) / 60)|int %}
              {% if hours > 0 %}
                {{ hours }}h {{ minutes }}m
              {% else %}
                {{ minutes }}m
              {% endif %}
            {% else %}
              —
            {% endif %}
          </div>
        </div>
      </div>
    </div>
    <div class="col-6 col-md-4">
      <div class="card h-100">
        <div class="card-body text-center">
          <div class="text-muted mb-2" style="font-size: 0.85rem;">
            <i class="bi bi-calendar-range"></i> Year Range
          </div>
          <div class="h5 mb-0">
            {% if stats.earliest_year and stats.latest_year %}
              {{ stats.earliest_year }} – {{ stats.latest_year }}
            {% else %}
              —
            {% endif %}
          </div>
        </div>
      </div>
    </div>
    <div class="col-6 col-md-4">
      <div class="card h-100">
        <div class="card-body text-center">
          <div class="text-muted mb-2" style="font-size: 0.85rem;">
            <i class="bi bi-calendar"></i> Avg Score
          </div>
          <div class="h5 mb-0">
            {% if stats.avg_stars %}
              <span class="badge badge-info">{{ (stats.avg_stars * 20)|round(0)|int }}%</span>
            {% else %}
              —
            {% endif %}
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Import Macro -->
  {% from "_album_category_section.html" import render_album_row %}

  <!-- Albums Section -->
  {% set total_discovered_albums = albums_by_category.album|selectattr('is_missing', 'equalto', False)|list|length %}
  {% set total_missing_albums = albums_by_category.album|selectattr('is_missing', 'equalto', True)|list|length %}
  {% set total_discovered_eps = albums_by_category.ep|selectattr('is_missing', 'equalto', False)|list|length %}
  {% set total_missing_eps = albums_by_category.ep|selectattr('is_missing', 'equalto', True)|list|length %}
  {% set total_discovered_singles = albums_by_category.single|selectattr('is_missing', 'equalto', False)|list|length %}
  {% set total_missing_singles = albums_by_category.single|selectattr('is_missing', 'equalto', True)|list|length %}
  {% set total_discovered_compilations = albums_by_category.compilation|selectattr('is_missing', 'equalto', False)|list|length %}
  {% set total_missing_compilations = albums_by_category.compilation|selectattr('is_missing', 'equalto', True)|list|length %}
  {% set total_discovered_live_albums = albums_by_category.live_album|selectattr('is_missing', 'equalto', False)|list|length %}
  {% set total_missing_live_albums = albums_by_category.live_album|selectattr('is_missing', 'equalto', True)|list|length %}
  {% set total_discovered_remix_albums = albums_by_category.remix_album|selectattr('is_missing', 'equalto', False)|list|length %}
  {% set total_missing_remix_albums = albums_by_category.remix_album|selectattr('is_missing', 'equalto', True)|list|length %}
  
  <div class="card mb-4" id="albums-section">
    <div class="card-header d-flex justify-content-between align-items-center">
      <h5 class="card-title mb-0">
        <i class="bi bi-vinyl-fill"></i> Albums
        <span class="badge bg-primary">{{ total_discovered_albums }}</span>
        {% if total_missing_albums > 0 %}
          <span class="badge bg-warning text-dark">{{ total_missing_albums }} missing</span>
        {% endif %}
      </h5>
      <div class="d-flex gap-2">
        {% if total_missing_albums > 0 %}
          <button class="btn btn-sm btn-outline-secondary toggle-missing-btn" onclick="toggleMissing('albums')" title="Show/hide missing albums" data-show="false">
            <i class="bi bi-eye-slash"></i> <span class="d-none d-sm-inline">Show Missing</span>
          </button>
        {% endif %}
        <button class="btn btn-sm btn-outline-info" onclick='searchMusicBrainzForAllReleases({{ artist_name|tojson }})' title="Search MusicBrainz for all artist releases to download">
          <i class="bi bi-cloud-download"></i> <span class="d-none d-sm-inline">Search All Releases</span>
        </button>
        <button class="btn btn-sm btn-outline-warning" onclick='checkMissingReleases({{ artist_name|tojson }})' title="Check MusicBrainz and add missing releases inline">
          <i class="bi bi-flag"></i> Check for Missing
        </button>
      </div>
    </div>
    <div class="table-responsive">
      <table class="table table-hover mb-0">
        <thead class="d-none d-md-table-header-group">
          <tr>
            <th style="width: 100px;">Art</th>
            <th>Title</th>
            <th class="text-center" style="width: 80px;">Year</th>
            <th class="text-center" style="width: 100px;">Status</th>
            <th class="text-center" style="width: 80px;">Tracks</th>
            <th class="text-center" style="width: 100px;">Avg ★</th>
            <th class="text-center" style="width: 150px;">Actions</th>
          </tr>
        </thead>
        <tbody class="albums-tbody">
          {% if albums_by_category.album|length > 0 %}
            {% for album in albums_by_category.album %}
              {{ render_album_row(album, artist_name, album.is_missing) }}
            {% endfor %}
          {% else %}
            <tr>
              <td colspan="7" class="text-center text-muted py-4">
                <i class="bi bi-info-circle"></i> No albums found
              </td>
            </tr>
          {% endif %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- Compilations Section -->
  <div class="card mb-4" id="compilations-section">
    <div class="card-header d-flex justify-content-between align-items-center">
      <div>
        <h5 class="card-title mb-0">
          <i class="bi bi-collection"></i> Compilations
          <span class="badge bg-primary">{{ total_discovered_compilations }}</span>
          {% if total_missing_compilations > 0 %}
            <span class="badge bg-warning text-dark">{{ total_missing_compilations }} missing</span>
          {% endif %}
        </h5>
        <p class="text-muted small mb-0 mt-1">Albums where this artist is featured (Various Artists, soundtracks, tributes, etc.)</p>
      </div>
      {% if total_missing_compilations > 0 %}
        <button class="btn btn-sm btn-outline-secondary toggle-missing-btn" onclick="toggleMissing('compilations')" title="Show/hide missing compilations" data-show="false">
          <i class="bi bi-eye-slash"></i> <span class="d-none d-sm-inline">Show Missing</span>
        </button>
      {% endif %}
    </div>
    <div class="table-responsive">
      <table class="table table-hover mb-0">
        <thead class="d-none d-md-table-header-group">
          <tr>
            <th style="width: 100px;">Art</th>
            <th>Title</th>
            <th class="text-center" style="width: 80px;">Year</th>
            <th class="text-center" style="width: 100px;">Status</th>
            <th class="text-center" style="width: 80px;">Tracks</th>
            <th class="text-center" style="width: 100px;">Avg ★</th>
            <th class="text-center" style="width: 150px;">Actions</th>
          </tr>
        </thead>
        <tbody class="compilations-tbody">
          {% if albums_by_category.compilation|length > 0 %}
            {% for album in albums_by_category.compilation %}
              {{ render_album_row(album, artist_name, album.is_missing) }}
            {% endfor %}
          {% else %}
            <tr>
              <td colspan="7" class="text-center text-muted py-4">
                <i class="bi bi-info-circle"></i> No compilations found
              </td>
            </tr>
          {% endif %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- Live Albums Section -->
  <div class="card mb-4" id="live-albums-section">
    <div class="card-header d-flex justify-content-between align-items-center">
      <h5 class="card-title mb-0">
        <i class="bi bi-broadcast"></i> Live Albums
        <span class="badge bg-primary">{{ total_discovered_live_albums }}</span>
        {% if total_missing_live_albums > 0 %}
          <span class="badge bg-warning text-dark">{{ total_missing_live_albums }} missing</span>
        {% endif %}
      </h5>
      <div class="d-flex gap-2">
        {% if total_missing_live_albums > 0 %}
          <button class="btn btn-sm btn-outline-secondary toggle-missing-btn" onclick="toggleMissing('live-albums')" title="Show/hide missing live albums" data-show="false">
            <i class="bi bi-eye-slash"></i> <span class="d-none d-sm-inline">Show Missing</span>
          </button>
        {% endif %}
        <button class="btn btn-sm btn-outline-info" onclick='searchMusicBrainzForAllReleases({{ artist_name|tojson }})' title="Search MusicBrainz for all artist releases to download">
          <i class="bi bi-cloud-download"></i> <span class="d-none d-sm-inline">Search All Releases</span>
        </button>
        <button class="btn btn-sm btn-outline-warning" onclick='checkMissingReleases({{ artist_name|tojson }})' title="Check MusicBrainz and add missing releases inline">
          <i class="bi bi-flag"></i> Check for Missing
        </button>
      </div>
    </div>
    <div class="table-responsive">
      <table class="table table-hover mb-0">
        <thead class="d-none d-md-table-header-group">
          <tr>
            <th style="width: 100px;">Art</th>
            <th>Title</th>
            <th class="text-center" style="width: 80px;">Year</th>
            <th class="text-center" style="width: 100px;">Status</th>
            <th class="text-center" style="width: 80px;">Tracks</th>
            <th class="text-center" style="width: 100px;">Avg ★</th>
            <th class="text-center" style="width: 150px;">Actions</th>
          </tr>
        </thead>
        <tbody class="live-albums-tbody">
          {% if albums_by_category.live_album|length > 0 %}
            {% for album in albums_by_category.live_album %}
              {{ render_album_row(album, artist_name, album.is_missing) }}
            {% endfor %}
          {% else %}
            <tr>
              <td colspan="7" class="text-center text-muted py-4">
                <i class="bi bi-info-circle"></i> No live albums found
              </td>
            </tr>
          {% endif %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- Remix Albums Section -->
  <div class="card mb-4" id="remix-albums-section">
    <div class="card-header d-flex justify-content-between align-items-center">
      <h5 class="card-title mb-0">
        <i class="bi bi-soundwave"></i> Remix Albums
        <span class="badge bg-primary">{{ total_discovered_remix_albums }}</span>
        {% if total_missing_remix_albums > 0 %}
          <span class="badge bg-warning text-dark">{{ total_missing_remix_albums }} missing</span>
        {% endif %}
      </h5>
      <div class="d-flex gap-2">
        {% if total_missing_remix_albums > 0 %}
          <button class="btn btn-sm btn-outline-secondary toggle-missing-btn" onclick="toggleMissing('remix-albums')" title="Show/hide missing remix albums" data-show="false">
            <i class="bi bi-eye-slash"></i> <span class="d-none d-sm-inline">Show Missing</span>
          </button>
        {% endif %}
        <button class="btn btn-sm btn-outline-info" onclick='searchMusicBrainzForAllReleases({{ artist_name|tojson }})' title="Search MusicBrainz for all artist releases to download">
          <i class="bi bi-cloud-download"></i> <span class="d-none d-sm-inline">Search All Releases</span>
        </button>
        <button class="btn btn-sm btn-outline-warning" onclick='checkMissingReleases({{ artist_name|tojson }})' title="Check MusicBrainz and add missing releases inline">
          <i class="bi bi-flag"></i> Check for Missing
        </button>
      </div>
    </div>
    <div class="table-responsive">
      <table class="table table-hover mb-0">
        <thead class="d-none d-md-table-header-group">
          <tr>
            <th style="width: 100px;">Art</th>
            <th>Title</th>
            <th class="text-center" style="width: 80px;">Year</th>
            <th class="text-center" style="width: 100px;">Status</th>
            <th class="text-center" style="width: 80px;">Tracks</th>
            <th class="text-center" style="width: 100px;">Avg ★</th>
            <th class="text-center" style="width: 150px;">Actions</th>
          </tr>
        </thead>
        <tbody class="remix-albums-tbody">
          {% if albums_by_category.remix_album|length > 0 %}
            {% for album in albums_by_category.remix_album %}
              {{ render_album_row(album, artist_name, album.is_missing) }}
            {% endfor %}
          {% else %}
            <tr>
              <td colspan="7" class="text-center text-muted py-4">
                <i class="bi bi-info-circle"></i> No remix albums found
              </td>
            </tr>
          {% endif %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- EPs Section -->
  <div class="card mb-4" id="eps-section">
    <div class="card-header d-flex justify-content-between align-items-center">
      <h5 class="card-title mb-0">
        <i class="bi bi-disc"></i> EPs
        <span class="badge bg-primary">{{ total_discovered_eps }}</span>
        {% if total_missing_eps > 0 %}
          <span class="badge bg-warning text-dark">{{ total_missing_eps }} missing</span>
        {% endif %}
      </h5>
      <div class="d-flex gap-2">
        {% if total_missing_eps > 0 %}
          <button class="btn btn-sm btn-outline-secondary toggle-missing-btn" onclick="toggleMissing('eps')" title="Show/hide missing EPs" data-show="false">
            <i class="bi bi-eye-slash"></i> <span class="d-none d-sm-inline">Show Missing</span>
          </button>
        {% endif %}
        <button class="btn btn-sm btn-outline-info" onclick='searchMusicBrainzForAllReleases({{ artist_name|tojson }})' title="Search MusicBrainz for all artist releases to download">
          <i class="bi bi-cloud-download"></i> <span class="d-none d-sm-inline">Search All Releases</span>
        </button>
        <button class="btn btn-sm btn-outline-warning" onclick='checkMissingReleases({{ artist_name|tojson }})' title="Check MusicBrainz and add missing releases inline">
          <i class="bi bi-flag"></i> Check for Missing
        </button>
      </div>
    </div>
    <div class="table-responsive">
      <table class="table table-hover mb-0">
        <thead class="d-none d-md-table-header-group">
          <tr>
            <th style="width: 100px;">Art</th>
            <th>Title</th>
            <th class="text-center" style="width: 80px;">Year</th>
            <th class="text-center" style="width: 100px;">Status</th>
            <th class="text-center" style="width: 80px;">Tracks</th>
            <th class="text-center" style="width: 100px;">Avg ★</th>
            <th class="text-center" style="width: 150px;">Actions</th>
          </tr>
        </thead>
        <tbody class="eps-tbody">
          {% if albums_by_category.ep|length > 0 %}
            {% for album in albums_by_category.ep %}
              {{ render_album_row(album, artist_name, album.is_missing) }}
            {% endfor %}
          {% else %}
            <tr>
              <td colspan="7" class="text-center text-muted py-4">
                <i class="bi bi-info-circle"></i> No EPs found
              </td>
            </tr>
          {% endif %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- Singles Section -->
  <div class="card mb-4" id="singles-section">
    <div class="card-header d-flex justify-content-between align-items-center">
      <h5 class="card-title mb-0">
        <i class="bi bi-music-note-beamed"></i> Singles
        <span class="badge bg-primary">{{ total_discovered_singles }}</span>
        {% if total_missing_singles > 0 %}
          <span class="badge bg-warning text-dark">{{ total_missing_singles }} missing</span>
        {% endif %}
      </h5>
      <div class="d-flex gap-2">
        {% if total_missing_singles > 0 %}
          <button class="btn btn-sm btn-outline-secondary toggle-missing-btn" onclick="toggleMissing('singles')" title="Show/hide missing singles" data-show="false">
            <i class="bi bi-eye-slash"></i> <span class="d-none d-sm-inline">Show Missing</span>
          </button>
        {% endif %}
        <button class="btn btn-sm btn-outline-info" onclick='searchMusicBrainzForAllReleases({{ artist_name|tojson }})' title="Search MusicBrainz for all artist releases to download">
          <i class="bi bi-cloud-download"></i> <span class="d-none d-sm-inline">Search All Releases</span>
        </button>
        <button class="btn btn-sm btn-outline-warning" onclick='checkMissingReleases({{ artist_name|tojson }})' title="Check MusicBrainz and add missing releases inline">
          <i class="bi bi-flag"></i> Check for Missing
        </button>
      </div>
    </div>
    <div class="table-responsive">
      <table class="table table-hover mb-0">
        <thead class="d-none d-md-table-header-group">
          <tr>
            <th style="width: 100px;">Art</th>
            <th>Title</th>
            <th class="text-center" style="width: 80px;">Year</th>
            <th class="text-center" style="width: 100px;">Status</th>
            <th class="text-center" style="width: 80px;">Tracks</th>
            <th class="text-center" style="width: 100px;">Avg ★</th>
            <th class="text-center" style="width: 150px;">Actions</th>
          </tr>
        </thead>
        <tbody class="singles-tbody">
          {% if albums_by_category.single|length > 0 %}
            {% for album in albums_by_category.single %}
              {{ render_album_row(album, artist_name, album.is_missing) }}
            {% endfor %}
          {% else %}
            <tr>
              <td colspan="7" class="text-center text-muted py-4">
                <i class="bi bi-info-circle"></i> No singles found
              </td>
            </tr>
          {% endif %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- Covered By Section -->
  <div class="card mb-4" id="artist-covers-section">
    <div class="card-header d-flex justify-content-between align-items-center">
      <h5 class="card-title mb-0">
        <i class="bi bi-music-note-list"></i> Covers of {{ artist_name }}'s Songs
      </h5>
      <small class="text-muted" id="artistCoveredByCount">Songs from other artists in your library that cover {{ artist_name }}'s work</small>
    </div>
    <div class="card-body p-0">
      <div id="artistCoveredByContainer">
        <div class="text-center py-3">
          <div class="spinner-border spinner-border-sm" role="status">
            <span class="visually-hidden">Loading...</span>
          </div>
          <small class="d-block mt-2 text-muted">Loading cover songs...</small>
        </div>
      </div>
    </div>
  </div>

  <!-- Appears On Section -->
  <div class="card mb-4" id="artist-appears-on-section">
    <div class="card-header d-flex justify-content-between align-items-center">
      <h5 class="card-title mb-0">
        <i class="bi bi-people"></i> Appears On
      </h5>
      <small class="text-muted">Featured on other artists' releases</small>
    </div>
    <div class="table-responsive">
      <table class="table table-hover mb-0">
        <thead class="d-none d-md-table-header-group">
          <tr>
            <th style="width: 100px;">Art</th>
            <th>Release</th>
            <th class="text-center" style="width: 80px;">Year</th>
            <th class="text-center" style="width: 80px;">Tracks</th>
            <th class="text-center" style="width: 100px;">Avg ★</th>
            <th class="text-center" style="width: 70px;">Open</th>
          </tr>
        </thead>
        <tbody>
          {% if appears_on_albums and appears_on_albums|length > 0 %}
            {% for entry in appears_on_albums %}
              <tr>
                <td class="text-center" style="width: 100px; padding: 0.5rem;">
                  {% if entry.album and entry.album_artist %}
                  <a href="{{ url_for('album_detail', artist=entry.album_artist, album=entry.album) }}">
                    <img src="{{ url_for('api_album_art', artist=entry.album_artist, album=entry.album) }}"
                         alt="{{ entry.album }}"
                         class="img-thumbnail"
                         style="width: 80px; height: 80px; object-fit: cover; border: 1px solid var(--border-color); background-color: #2a2a2a;"
                         onerror="this.src='/api/album-art-placeholder'">
                  </a>
                  {% else %}
                  <img src="/api/album-art-placeholder"
                       alt="{{ entry.album }}"
                       class="img-thumbnail"
                       style="width: 80px; height: 80px; object-fit: cover; border: 1px solid var(--border-color); background-color: #2a2a2a;">
                  {% endif %}
                </td>
                <td>
                  <div class="d-flex flex-column justify-content-center" style="min-height: 80px;">
                    {% if entry.album and entry.album_artist %}
                    <a href="{{ url_for('album_detail', artist=entry.album_artist, album=entry.album) }}" class="text-decoration-none fw-semibold">
                      {{ entry.album }}
                    </a>
                    {% else %}
                    <span class="fw-semibold">{{ entry.album or '—' }}</span>
                    {% endif %}
                    {% if entry.album_artist %}
                    <a href="{{ url_for('artist_detail', name=entry.album_artist) }}" class="text-decoration-none small text-muted mt-1">
                      {{ entry.album_artist }}
                    </a>
                    {% else %}
                    <span class="small text-muted mt-1">—</span>
                    {% endif %}
                  </div>
                </td>
                <td class="text-center">{{ entry.album_year or '—' }}</td>
                <td class="text-center">{{ entry.track_count or 0 }}</td>
                <td class="text-center">
                  {% if entry.avg_stars %}
                    <span class="badge bg-info">{{ entry.avg_stars|round(2) }}★</span>
                  {% else %}
                    <span class="text-muted">—</span>
                  {% endif %}
                </td>
                <td class="text-center">
                  <a class="btn btn-sm btn-outline-secondary" href="{{ url_for('album_detail', artist=entry.album_artist, album=entry.album) }}" title="View album">
                    <i class="bi bi-arrow-right"></i>
                  </a>
                </td>
              </tr>
            {% endfor %}
          {% else %}
            <tr>
              <td colspan="6" class="text-center text-muted py-4">
                <i class="bi bi-info-circle"></i> No featured-release appearances found
              </td>
            </tr>
          {% endif %}
        </tbody>
      </table>
    </div>
  </div>

</div>

<!-- Download Search Modal (qBittorrent & Soulseek) -->
{% if qbit_config.enabled or slskd_config.enabled %}
<div class="modal fade" id="downloadModal" tabindex="-1" aria-labelledby="downloadModalLabel" aria-hidden="true">
  <div class="modal-dialog modal-xl">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title" id="downloadModalLabel">
          <i class="bi bi-download"></i> Download Album: <span id="downloadArtistName"></span>
        </h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
      </div>
      <div class="modal-body">
        <!-- Tabs Navigation -->
        <ul class="nav nav-tabs mb-3" id="downloadTabs" role="tablist">
          {% if qbit_config.enabled and qbit_config.web_url %}
          <li class="nav-item" role="presentation">
            <button class="nav-link active" id="qbit-tab" data-bs-toggle="tab" data-bs-target="#qbit-content" 
                    type="button" role="tab" aria-controls="qbit-content" aria-selected="true">
              <i class="bi bi-cloud-download"></i> qBittorrent
            </button>
          </li>
          {% endif %}
          {% if slskd_config.enabled %}
          <li class="nav-item" role="presentation">
            <button class="nav-link {% if not (qbit_config.enabled and qbit_config.web_url) %}active{% endif %}" 
                    id="slskd-tab" data-bs-toggle="tab" data-bs-target="#slskd-content" 
                    type="button" role="tab" aria-controls="slskd-content" 
                    aria-selected="{% if not (qbit_config.enabled and qbit_config.web_url) %}true{% else %}false{% endif %}">
              <i class="bi bi-music-note-list"></i> Soulseek
            </button>
          </li>
          {% endif %}
        </ul>

        <!-- Tabs Content -->
        <div class="tab-content" id="downloadTabsContent">
          <!-- qBittorrent Tab -->
          {% if qbit_config.enabled and qbit_config.web_url %}
          <div class="tab-pane fade show active" id="qbit-content" role="tabpanel" aria-labelledby="qbit-tab">
            <div class="mb-3">
              <div class="input-group">
                <input type="text" class="form-control" id="qbitSearchInput" placeholder="Search query...">
                <button class="btn btn-primary" onclick="performQbitSearch()">
                  <i class="bi bi-search"></i> Search
                </button>
              </div>
              <div class="form-text">Search for albums, discographies, or specific releases</div>
            </div>
            
            <div id="qbitLoading" class="text-center py-4" style="display: none;">
              <div class="spinner-border text-primary" role="status">
                <span class="visually-hidden">Searching...</span>
              </div>
              <p class="mt-2">Searching qBittorrent...</p>
            </div>
            
            <div id="qbitResults"></div>
          </div>
          {% endif %}

          <!-- Soulseek Tab -->
          {% if slskd_config.enabled %}
          <div class="tab-pane fade {% if not (qbit_config.enabled and qbit_config.web_url) %}show active{% endif %}" 
               id="slskd-content" role="tabpanel" aria-labelledby="slskd-tab">
            <div class="mb-3">
              <div class="input-group">
                <input type="text" class="form-control" id="slskdSearchInput" placeholder="Search query...">
                <button class="btn btn-primary" onclick="performSlskdSearch()">
                  <i class="bi bi-search"></i> Search
                </button>
              </div>
              <div class="form-text">Search for albums or tracks on Soulseek network</div>
            </div>
            
            <!-- Track Queue Section -->
            <div id="slskdTracksContainer" class="mb-3"></div>
            
            <div id="slskdLoading" class="text-center py-4" style="display: none;">
              <div class="spinner-border text-primary" role="status">
                <span class="visually-hidden">Searching...</span>
              </div>
              <p class="mt-2">Searching Soulseek...</p>
            </div>
            
            <div id="slskdResults"></div>
          </div>
          {% endif %}
        </div>
      </div>
    </div>
  </div>
</div>
{% endif %}

<!-- Comprehensive Track Edit Modal for Artist Page -->
<div class="modal fade" id="editTrackFromArtistModal" tabindex="-1" aria-hidden="true">
    <div class="modal-dialog modal-lg">
        <div class="modal-content">
            <div class="modal-header">
                <h5 class="modal-title">
                    <i class="bi bi-pencil-square"></i> Edit Track: <span id="editArtistTrackTitle"></span>
                </h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
            </div>
            <div class="modal-body">
                <form id="editArtistTrackForm">
                    <input type="hidden" id="editArtistTrackId" name="track_id">
                    
                    <!-- Basic Info -->
                    <div class="row g-3 mb-4">
                        <div class="col-md-12">
                            <label for="editArtistTrackTitleField" class="form-label">Title</label>
                            <input type="text" class="form-control" id="editArtistTrackTitleField" name="title" required>
                        </div>
                        <div class="col-md-6">
                            <label for="editArtistTrackArtistField" class="form-label">Artist</label>
                            <input type="text" class="form-control" id="editArtistTrackArtistField" name="artist">
                        </div>
                        <div class="col-md-6">
                            <label for="editArtistTrackAlbumField" class="form-label">Album</label>
                            <input type="text" class="form-control" id="editArtistTrackAlbumField" name="album">
                        </div>
                    </div>
                    
                    <!-- Metadata -->
                    <div class="row g-3 mb-4">
                        <div class="col-md-3">
                            <label for="editArtistTrackYearField" class="form-label">Year</label>
                            <input type="number" class="form-control" id="editArtistTrackYearField" name="year" placeholder="YYYY">
                        </div>
                        <div class="col-md-3">
                            <label for="editArtistTrackStarsField" class="form-label">Rating</label>
                            <select class="form-select" id="editArtistTrackStarsField" name="stars">
                                <option value="0">☆☆☆☆☆ (0)</option>
                                <option value="1">★☆☆☆☆ (1)</option>
                                <option value="2">★★☆☆☆ (2)</option>
                                <option value="3">★★★☆☆ (3)</option>
                                <option value="4">★★★★☆ (4)</option>
                                <option value="5">★★★★★ (5)</option>
                            </select>
                        </div>
                        <div class="col-md-3">
                            <label for="editArtistTrackSingleField" class="form-label">Is Single</label>
                            <select class="form-select" id="editArtistTrackSingleField" name="is_single">
                                <option value="0">No</option>
                                <option value="1">Yes</option>
                            </select>
                        </div>
                        <div class="col-md-3">
                            <label for="editArtistTrackConfidenceField" class="form-label">Confidence</label>
                            <select class="form-select" id="editArtistTrackConfidenceField" name="single_confidence">
                                <option value="low">Low</option>
                                <option value="medium">Medium</option>
                                <option value="high">High</option>
                            </select>
                        </div>
                    </div>
                    
                    <!-- Recommended Genres Section -->
                    <div class="mb-4" id="artistRecommendedGenresSection" style="display: none;">
                        <div class="alert alert-info" style="padding: 0.75rem; margin-bottom: 1rem;">
                            <h6 class="mb-2"><i class="bi bi-stars"></i> Recommended Genres from Last.fm & Discogs</h6>
                            <div id="artistRecommendedGenresDisplay" class="d-flex flex-wrap gap-2">
                                <span class="text-muted small">Loading...</span>
                            </div>
                        </div>
                    </div>
                    
                    <!-- Genres -->
                    <div class="mb-4">
                        <label for="editArtistTrackGenresField" class="form-label">Genres</label>
                        <div id="editArtistTrackGenresDisplay" class="mb-2">
                            <!-- Genres badges will be inserted here -->
                        </div>
                        <div class="input-group mb-2">
                            <input type="text" class="form-control" id="editArtistTrackGenreInput" placeholder="Add genre (e.g., Rock, Jazz)" list="editArtistGenreDatalist">
                            <button class="btn btn-outline-success" type="button" onclick="addEditArtistTrackGenre()">
                                <i class="bi bi-plus-circle"></i> Add
                            </button>
                        </div>
                        <datalist id="editArtistGenreDatalist">
                            <option value="Rock"><option value="Pop"><option value="Jazz"><option value="Classical">
                            <option value="Hip-Hop"><option value="Electronic"><option value="Blues"><option value="Country">
                            <option value="R&B"><option value="Soul"><option value="Funk"><option value="Metal">
                            <option value="Punk"><option value="Alternative"><option value="Indie">
                        </datalist>
                        <input type="hidden" id="editArtistTrackGenresField" name="genres">
                    </div>
                    
                    <!-- Additional Fields -->
                    <div class="row g-3 mb-4">
                        <div class="col-md-6">
                            <label for="editArtistTrackAlbumArtistField" class="form-label">Album Artist</label>
                            <input type="text" class="form-control" id="editArtistTrackAlbumArtistField" name="album_artist">
                        </div>
                        <div class="col-md-6">
                            <label for="editArtistTrackComposerField" class="form-label">Composer</label>
                            <input type="text" class="form-control" id="editArtistTrackComposerField" name="composer">
                        </div>
                        <div class="col-md-3">
                            <label for="editArtistTrackTrackNumberField" class="form-label">Track Number</label>
                            <input type="text" class="form-control" id="editArtistTrackTrackNumberField" name="track_number" placeholder="e.g., 1 or 1/12">
                        </div>
                        <div class="col-md-3">
                            <label for="editArtistTrackDiscNumberField" class="form-label">Disc Number</label>
                            <input type="number" class="form-control" id="editArtistTrackDiscNumberField" name="disc_number">
                        </div>
                        <div class="col-md-6">
                            <label for="editArtistTrackMBIDField" class="form-label">MusicBrainz ID</label>
                            <input type="text" class="form-control" id="editArtistTrackMBIDField" name="mbid" placeholder="e.g., 579fc21e-40d4-4d72-835c-659e2a4108e4">
                        </div>
                    </div>

                      <!-- Advanced Navidrome Fields -->
                      <div class="accordion mb-4" id="editArtistTrackAdvancedAccordion">
                        <div class="accordion-item">
                          <h2 class="accordion-header" id="editArtistTrackAdvancedHeading">
                            <button class="accordion-button collapsed" type="button" data-bs-toggle="collapse" data-bs-target="#editArtistTrackAdvancedCollapse" aria-expanded="false" aria-controls="editArtistTrackAdvancedCollapse">
                              Advanced Navidrome Fields
                            </button>
                          </h2>
                          <div id="editArtistTrackAdvancedCollapse" class="accordion-collapse collapse" aria-labelledby="editArtistTrackAdvancedHeading" data-bs-parent="#editArtistTrackAdvancedAccordion">
                            <div class="accordion-body">
                              <div id="editArtistTrackAdvancedFields" class="row g-3"></div>
                            </div>
                          </div>
                        </div>
                      </div>
                    
                    <!-- Comment -->
                    <div class="mb-3">
                        <label for="editArtistTrackCommentField" class="form-label">Comment</label>
                        <textarea class="form-control" id="editArtistTrackCommentField" name="comment" rows="2" placeholder="Add notes or comments"></textarea>
                    </div>
                </form>
            </div>
            <div class="modal-footer">
                <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                <button type="button" class="btn btn-primary" onclick="saveArtistEditedTrack()">
                    <i class="bi bi-check-circle"></i> Save Changes
                </button>
            </div>
        </div>
    </div>
</div>

<!-- MusicBrainz Release Search Modal -->
<div class="modal fade" id="musicBrainzModal" tabindex="-1" aria-labelledby="musicBrainzModalLabel" aria-hidden="true">
  <div class="modal-dialog modal-xl">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title" id="musicBrainzModalLabel">
          <i class="bi bi-music-note-list"></i> MusicBrainz Release Search
        </h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
      </div>
      <div class="modal-body">
        <div id="mbSearchInfo" class="alert alert-info mb-3" style="display:none;">
          Searching <strong id="mbSearchArtist"></strong> - <strong id="mbSearchAlbum"></strong>
        </div>
        
        <div id="mbSearchStatus" class="text-center py-4" style="display:none;">
          <div class="spinner-border text-primary" role="status">
            <span class="visually-hidden">Loading...</span>
          </div>
          <p class="mt-2 text-muted">Searching MusicBrainz...</p>
        </div>
        
        <div id="mbSearchError" class="alert alert-danger" style="display:none;"></div>

        <div id="mbSearchRetry" class="mt-2" style="display:none;">
          <div class="d-flex gap-2 align-items-end flex-wrap">
            <div class="flex-fill">
              <label for="mbRetryArtist" class="form-label form-label-sm mb-1">Artist</label>
              <input type="text" class="form-control form-control-sm" id="mbRetryArtist" placeholder="Artist name">
            </div>
            <div class="flex-fill">
              <label for="mbRetryAlbum" class="form-label form-label-sm mb-1">Album</label>
              <input type="text" class="form-control form-control-sm" id="mbRetryAlbum" placeholder="Album name">
            </div>
            <div class="flex-shrink-0">
              <button class="btn btn-sm btn-primary" onclick="mbRetrySearch()">
                <i class="bi bi-search"></i> Retry
              </button>
            </div>
          </div>
        </div>

        <div id="mbSearchResults"></div>
      </div>
    </div>
  </div>
</div>
<!-- End MusicBrainz Search Modal -->

<script>
// Constants for slskd search polling
const SLSKD_MAX_POLL_ATTEMPTS = 60;
const SLSKD_POLL_INTERVAL_MS = 2000;
const BYTES_TO_MB = 1024 * 1024;

// Event listener for Soulseek tab - show track queue option when tab is clicked
document.addEventListener('DOMContentLoaded', function() {
  const slskdTab = document.getElementById('slskd-tab');
  if (slskdTab) {
    slskdTab.addEventListener('shown.bs.tab', function() {
      if (currentDownloadAlbum.album) {
        showSlskdTrackQueueOption();
      }
    });
  }
});

// Fallback functions if genre-utils.js fails to load
if (typeof toggleGenreCheckbox === 'undefined') {
    window.toggleGenreCheckbox = function(containerId, buttonId) {
        const button = document.getElementById(buttonId);
        const container = document.getElementById(containerId);
        if (!container || !button) return;
        const checkboxes = container.querySelectorAll('input[type="checkbox"]');
        const checkedBoxes = Array.from(checkboxes).filter(cb => cb.checked);
        if (checkedBoxes.length > 0) {
            button.style.display = 'inline-block';
            button.textContent = `Remove ${checkedBoxes.length} Selected Genre${checkedBoxes.length > 1 ? 's' : ''}`;
        } else {
            button.style.display = 'none';
        }
    };
}

if (typeof getSelectedGenres === 'undefined') {
    window.getSelectedGenres = function(containerId) {
        const container = document.getElementById(containerId);
        if (!container) return [];
        return Array.from(container.querySelectorAll('input[type="checkbox"]:checked')).map(cb => cb.value);
    };
}

if (typeof handleGenreRemoval === 'undefined') {
    window.handleGenreRemoval = function(artistName, albumName, genres, contextType) {
        // Fallback handler for genre removal
        alert(`Would remove genres: ${genres.join(', ')}`);
    };
}

// ===== Artist IDs Modal Functions (defined early for onclick handlers) =====
function openEditArtistIdsModal() {
  const artistName = {{ artist_name|tojson }};
  const musicbrainzId = document.getElementById('musicbrainzArtistId').value;
  const discogsId = (function() { const el = document.getElementById('discogsArtistId'); return el ? el.value : ''; })();
  
  const modalHtml = `
    <div class="modal fade" id="editArtistIdsModal" tabindex="-1">
      <div class="modal-dialog">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title"><i class="bi bi-pencil"></i> Edit Artist IDs</h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body">
            <div class="mb-3">
              <label for="editMusicbrainzArtistId" class="form-label">MusicBrainz Artist ID</label>
              <input type="text" class="form-control" id="editMusicbrainzArtistId" value="${escapeHtml(musicbrainzId)}" placeholder="e.g., a74b1b7f-71a5-4011-9441-d0b5e4122711">
              <div class="form-text">
                <a href="https://musicbrainz.org/search?query=${encodeURIComponent(artistName)}&type=artist" target="_blank">Search MusicBrainz</a> to find the artist ID
              </div>
            </div>
            <div class="mb-3">
              <label for="editDiscogsArtistId" class="form-label">Discogs Artist ID</label>
              <input type="text" class="form-control" id="editDiscogsArtistId" value="${escapeHtml(discogsId)}" placeholder="e.g., 123456">
              <div class="form-text">
                <a href="https://www.discogs.com/search/?q=${encodeURIComponent(artistName)}&type=artist" target="_blank">Search Discogs</a> to find the artist ID
              </div>
            </div>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
            <button type="button" class="btn btn-outline-info" onclick="lookupAndSaveArtistIds(this)">
              <i class="bi bi-cloud-download"></i> Lookup and Save
            </button>
            <button type="button" class="btn btn-primary" onclick="saveArtistIds()">
              <i class="bi bi-save"></i> Save Changes
            </button>
          </div>
        </div>
      </div>
    </div>
  `;
  
  const existingModal = document.getElementById('editArtistIdsModal');
  if (existingModal) existingModal.remove();
  
  document.body.insertAdjacentHTML('beforeend', modalHtml);
  
  const modal = new bootstrap.Modal(document.getElementById('editArtistIdsModal'));
  modal.show();
}

function saveArtistIds() {
  const artistName = {{ artist_name|tojson }};
  const musicbrainzId = document.getElementById('editMusicbrainzArtistId').value.trim();
  const discogsId = document.getElementById('editDiscogsArtistId').value.trim();
  const saveBtn = document.querySelector('#editArtistIdsModal .btn.btn-primary');
  const originalBtnHtml = saveBtn ? saveBtn.innerHTML : null;
  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" style="width:0.8rem;height:0.8rem;"></span> Saving...';
  }
  
  fetch('/api/artist/update-ids', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      artist: artistName,
      lastfm_artist_mbid: musicbrainzId,
      musicbrainz_artist_id: musicbrainzId,
      discogs_artist_id: discogsId
    })
  })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        document.getElementById('musicbrainzArtistId').value = musicbrainzId;
        if (document.getElementById('discogsArtistId')) {
          document.getElementById('discogsArtistId').value = discogsId;
        }
        
        const modalInstance = bootstrap.Modal.getInstance(document.getElementById('editArtistIdsModal'));
        if (modalInstance) modalInstance.hide();
        
        alert('✅ Artist IDs updated successfully!');
        setTimeout(() => location.reload(), 1000);
      } else {
        alert('❌ Error: ' + (data.error || 'Failed to update IDs'));
      }
    })
    .catch(err => {
      console.error('Error:', err);
      alert('❌ Network error: ' + err.message);
    })
    .finally(() => {
      if (saveBtn) {
        saveBtn.disabled = false;
        saveBtn.innerHTML = originalBtnHtml;
      }
    });
}

function lookupAndSaveArtistIds(button) {
  const artistName = {{ artist_name|tojson }};
  const originalHtml = button.innerHTML;
  button.disabled = true;
  button.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" style="width:0.8rem;height:0.8rem;"></span> Looking up...';

  fetch('/api/artist/lookup-ids', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ artist: artistName })
  })
    .then(r => r.json())
    .then(data => {
      if (!data.success) {
        alert('❌ Error: ' + (data.error || 'Lookup failed'));
        return;
      }

      const mbid = data.musicbrainz_artist_id || '';
      const discogs = data.discogs_artist_id || '';

      const editMb = document.getElementById('editMusicbrainzArtistId');
      const editDc = document.getElementById('editDiscogsArtistId');
      if (editMb && mbid) editMb.value = mbid;
      if (editDc && discogs) editDc.value = discogs;

      const readonlyMb = document.getElementById('musicbrainzArtistId');
      const readonlyDc = document.getElementById('discogsArtistId');
      if (readonlyMb && mbid) readonlyMb.value = mbid;
      if (readonlyDc && discogs) readonlyDc.value = discogs;

      alert('✅ Lookup complete and IDs saved to artist tracks.');
    })
    .catch(err => {
      alert('❌ Network error: ' + err.message);
    })
    .finally(() => {
      button.disabled = false;
      button.innerHTML = originalHtml;
    });
}

// Store current album info for modal
let currentDownloadAlbum = {
  artist: null,
  album: null
};

function openDownloadSearch(artistName, albumName) {
  // Store for track queueing
  currentDownloadAlbum = { artist: artistName, album: albumName };
  
  document.getElementById('downloadArtistName').textContent = artistName + (albumName ? ' - ' + albumName : '');
  
  // Set search query for both tabs
  const query = albumName ? artistName + ' ' + albumName : artistName;
  
  const qbitInput = document.getElementById('qbitSearchInput');
  if (qbitInput) {
    qbitInput.value = query;
    document.getElementById('qbitResults').innerHTML = '';
  }
  
  const slskdInput = document.getElementById('slskdSearchInput');
  if (slskdInput) {
    slskdInput.value = query;
    document.getElementById('slskdResults').innerHTML = '';
    // Clear track queue section
    document.getElementById('slskdTracksContainer').innerHTML = '';
  }
  
  const modal = new bootstrap.Modal(document.getElementById('downloadModal'));
  modal.show();
  
  // Auto-search on the active tab
  setTimeout(() => {
    const activeTab = document.querySelector('#downloadTabs .nav-link.active');
    if (activeTab && activeTab.id === 'qbit-tab') {
      performQbitSearch();
    } else if (activeTab && activeTab.id === 'slskd-tab') {
      // Show track queue option for Soulseek
      if (albumName) {
        showSlskdTrackQueueOption();
      }
      performSlskdSearch();
    }
  }, 300);
}

function showSlskdTrackQueueOption() {
  const container = document.getElementById('slskdTracksContainer');
  if (!container) return;
  
  container.innerHTML = `
    <div class="alert alert-info d-flex justify-content-between align-items-center mb-3">
      <div>
        <i class="bi bi-info-circle"></i> 
        Queue individual tracks from <strong>${escapeHtml(currentDownloadAlbum.album)}</strong> to Soulseek download queue
      </div>
      <button class="btn btn-sm btn-success" onclick="queueAlbumTracksToSlskd()">
        <i class="bi bi-plus-circle"></i> Queue Tracks
      </button>
    </div>
    <div id="slskdTrackQueueStatus"></div>
  `;
}

function queueAlbumTracksToSlskd() {
  if (!currentDownloadAlbum.album) {
    alert('No album selected');
    return;
  }
  
  const statusDiv = document.getElementById('slskdTrackQueueStatus');
  statusDiv.innerHTML = `
    <div class="text-center py-3">
      <div class="spinner-border spinner-border-sm text-primary" role="status">
        <span class="visually-hidden">Loading...</span>
      </div>
      <p class="mt-2">Fetching album tracklist...</p>
    </div>
  `;
  
  // Fetch album tracklist
  const params = new URLSearchParams({
    artist: currentDownloadAlbum.artist,
    album: currentDownloadAlbum.album
  });
  
  fetch(`/api/album/tracklist?${params}`)
    .then(response => response.json())
    .then(data => {
      if (data.error || !data.tracks || data.tracks.length === 0) {
        statusDiv.innerHTML = `
          <div class="alert alert-danger">
            <i class="bi bi-exclamation-triangle"></i> Could not fetch tracklist: ${escapeHtml(data.error || 'No tracks found')}
          </div>
        `;
        return;
      }
      
      const tracks = data.tracks;
      let queuedCount = 0;
      let html = `
        <div class="alert alert-info">
          <strong>Adding ${tracks.length} track${tracks.length !== 1 ? 's' : ''} to download queue...</strong>
        </div>
        <div class="table-responsive">
          <table class="table table-sm">
            <thead>
              <tr>
                <th style="width: 40px;"></th>
                <th>Track</th>
                <th class="text-center">Status</th>
              </tr>
            </thead>
            <tbody id="trackQueueTable">
      `;
      
      // Display all tracks first
      tracks.forEach((track, idx) => {
        const trackTitle = track.title || `Track ${idx + 1}`;
        html += `
          <tr id="queue-track-${idx}">
            <td class="text-center"><small class="text-muted">${idx + 1}</small></td>
            <td><small>${escapeHtml(trackTitle)}</small></td>
            <td class="text-center"><small id="queue-status-${idx}" class="text-muted">pending...</small></td>
          </tr>
        `;
      });
      
      html += `
            </tbody>
          </table>
        </div>
      `;
      
      statusDiv.innerHTML = html;
      
      // Queue tracks sequentially
      let index = 0;
      const queueNextTrack = () => {
        if (index >= tracks.length) {
          // Update summary
          const summary = document.querySelector('.alert-info strong');
          if (summary) {
            summary.textContent = `✓ Added ${queuedCount} of ${tracks.length} track${tracks.length !== 1 ? 's' : ''} to download queue`;
          }
          return;
        }
        
        const track = tracks[index];
        const statusEl = document.getElementById(`queue-status-${index}`);
        const trackTitle = track.title || `Track ${index + 1}`;
        
        statusEl.textContent = 'queuing...';
        statusEl.classList.add('text-warning');
        
        fetch('/api/queue/add', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({
            artist: currentDownloadAlbum.artist,
            title: trackTitle,
            album: currentDownloadAlbum.album,
            source: 'soulseek',
            priority: 5
          })
        })
        .then(response => response.json())
        .then(queueData => {
          if (queueData.success || queueData.queue_id) {
            queuedCount++;
            statusEl.textContent = '✓ queued';
            statusEl.classList.remove('text-warning');
            statusEl.classList.add('text-success');
          } else {
            throw new Error(queueData.error || 'Unknown error');
          }
          index++;
          queueNextTrack();
        })
        .catch(error => {
          statusEl.textContent = '✗ failed';
          statusEl.classList.remove('text-warning');
          statusEl.classList.add('text-danger');
          index++;
          queueNextTrack();
        });
      };
      
      queueNextTrack();
    })
    .catch(error => {
      statusDiv.innerHTML = `
        <div class="alert alert-danger">
          <i class="bi bi-exclamation-triangle"></i> Network error: ${escapeHtml(error.message)}
        </div>
      `;
    });
}

function performSlskdSearch() {
  const query = document.getElementById('slskdSearchInput').value;
  if (!query) return;
  
  document.getElementById('slskdLoading').style.display = 'block';
  document.getElementById('slskdResults').innerHTML = '';
  
  fetch('/api/slskd/search', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({ query: query })
  })
  .then(response => response.json())
  .then(data => {
    if (data.error) {
      document.getElementById('slskdLoading').style.display = 'none';
      document.getElementById('slskdResults').innerHTML = `
        <div class="alert alert-danger">
          <i class="bi bi-exclamation-triangle"></i> Error: ${escapeHtml(data.error)}
        </div>
      `;
      return;
    }

    if (data.slotBusy) {
      document.getElementById('slskdResults').innerHTML = `
        <div class="alert alert-warning">
          <i class="bi bi-clock"></i> <strong>Soulseek search slot is busy.</strong>
          An active search is in progress. Retrying automatically…
        </div>
      `;
      // Poll /api/slskd/search-slot until free, then retry
      const slotPoll = setInterval(() => {
        fetch('/api/slskd/search-slot')
          .then(r => r.json())
          .then(slotData => {
            if (slotData.slotFree) {
              clearInterval(slotPoll);
              performSlskdSearch();
            }
          })
          .catch(() => {});
      }, 2000);
      return;
    }
    
    const searchId = data.searchId;
    // Poll for results
    pollSlskdResults(searchId, query);
  })
  .catch(error => {
    document.getElementById('slskdLoading').style.display = 'none';
    document.getElementById('slskdResults').innerHTML = `
      <div class="alert alert-danger">
        <i class="bi bi-exclamation-triangle"></i> Network error: ${escapeHtml(error.message)}
      </div>
    `;
  });
}

function pollSlskdResults(searchId, query, pollCount = 0) {
  if (pollCount > SLSKD_MAX_POLL_ATTEMPTS) {
    document.getElementById('slskdLoading').style.display = 'none';
    document.getElementById('slskdResults').innerHTML = `
      <div class="alert alert-warning">
        <i class="bi bi-clock"></i> Search timed out. Try again or check Downloads page.
      </div>
    `;
    return;
  }
  
  fetch(`/api/slskd/search/${searchId}`)
    .then(response => response.json())
    .then(data => {
      if (!data.isComplete) {
        setTimeout(() => pollSlskdResults(searchId, query, pollCount + 1), SLSKD_POLL_INTERVAL_MS);
        return;
      }
      
      document.getElementById('slskdLoading').style.display = 'none';
      
      if (data.fileCount === 0 || !data.results || data.results.length === 0) {
        document.getElementById('slskdResults').innerHTML = `
          <div class="alert alert-info">
            <i class="bi bi-info-circle"></i> No results found. Try a different search query.
          </div>
        `;
        return;
      }
      
      let html = `
        <div class="table-responsive">
          <table class="table table-hover">
            <thead>
              <tr>
                <th>File</th>
                <th class="text-center">User</th>
                <th class="text-center">Size</th>
                <th class="text-center">Bitrate</th>
                <th class="text-center">Action</th>
              </tr>
            </thead>
            <tbody>
      `;
      
      data.results.forEach((result, idx) => {
        const sizeMB = result.size_mb || (result.size ? (result.size / BYTES_TO_MB).toFixed(2) : 'N/A');
        const bitrate = result.bitrate || 'unknown';
        
        html += `
          <tr>
            <td>
              <div class="small" style="max-width: 500px; overflow: hidden; text-overflow: ellipsis;">
                ${escapeHtml(result.filename || 'Unknown')}
              </div>
            </td>
            <td class="text-center">
              <small class="text-muted">${escapeHtml(result.username || 'N/A')}</small>
            </td>
            <td class="text-center">${sizeMB} MB</td>
            <td class="text-center">
              <small class="text-muted">${bitrate}</small>
            </td>
            <td class="text-center">
              <button class="btn btn-sm btn-success" 
                      onclick="downloadSlskdFile('${escapeJsString(result.username)}', '${escapeJsString(result.filename)}', ${parseInt(result.size) || 0})">
                <i class="bi bi-download"></i> Download
              </button>
            </td>
          </tr>
        `;
      });
      
      html += `
            </tbody>
          </table>
        </div>
        <div class="text-muted small mt-2">
          Found ${data.results.length} result(s)
        </div>
      `;
      
      document.getElementById('slskdResults').innerHTML = html;
    })
    .catch(error => {
      document.getElementById('slskdLoading').style.display = 'none';
      document.getElementById('slskdResults').innerHTML = `
        <div class="alert alert-danger">
          <i class="bi bi-exclamation-triangle"></i> Error: ${escapeHtml(error.message)}
        </div>
      `;
    });
}

function downloadSlskdFile(username, filename, size) {
  if (!confirm('Download this file from Soulseek?')) return;
  
  fetch('/api/slskd/download-single', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      username: username,
      filename: filename,
      size: parseInt(size)
    })
  })
  .then(response => response.json())
  .then(data => {
    if (data.error) {
      alert('❌ Error: ' + data.error);
    } else {
      alert('✅ Download started! Check the Downloads page for progress.');
    }
  })
  .catch(error => {
    alert('❌ Network error: ' + error.message);
  });
}

function performQbitSearch() {
  const query = document.getElementById('qbitSearchInput').value;
  if (!query) return;
  
  document.getElementById('qbitLoading').style.display = 'block';
  document.getElementById('qbitResults').innerHTML = '';
  
  fetch('/api/qbittorrent/search', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({ query: query })
  })
  .then(response => response.json())
  .then(data => {
    document.getElementById('qbitLoading').style.display = 'none';
    
    if (data.error) {
      document.getElementById('qbitResults').innerHTML = `
        <div class="alert alert-danger">
          <i class="bi bi-exclamation-triangle"></i> Error: ${data.error}
        </div>
      `;
      return;
    }
    
    const results = data.results || [];
    
    if (results.length === 0) {
      document.getElementById('qbitResults').innerHTML = `
        <div class="alert alert-info">
          <i class="bi bi-info-circle"></i> No results found. Try a different search query.
        </div>
      `;
      return;
    }
    
    let html = `
      <div class="table-responsive">
        <table class="table table-hover">
          <thead>
            <tr>
              <th>Name</th>
              <th class="text-center">Size</th>
              <th class="text-center">Seeds</th>
              <th class="text-center">Peers</th>
              <th class="text-center">Action</th>
            </tr>
          </thead>
          <tbody>
    `;
    
    results.forEach((result, idx) => {
      const size = formatBytes(result.fileSize || 0);
      const seedClass = result.nbSeeders > 10 ? 'text-success' : (result.nbSeeders > 0 ? 'text-warning' : 'text-danger');
      
      html += `
        <tr>
          <td>
            <div class="small" style="max-width: 500px; overflow: hidden; text-overflow: ellipsis;">
              ${escapeHtml(result.fileName || 'Unknown')}
            </div>
            <div class="text-muted" style="font-size: 0.75rem;">
              ${escapeHtml(result.siteUrl || '')}
            </div>
          </td>
          <td class="text-center">${size}</td>
          <td class="text-center ${seedClass}">
            <i class="bi bi-arrow-up-circle"></i> ${result.nbSeeders || 0}
          </td>
          <td class="text-center">
            <i class="bi bi-arrow-down-circle"></i> ${result.nbLeechers || 0}
          </td>
          <td class="text-center">
            <button class="btn btn-sm btn-success" onclick="addTorrent('${escapeJsString(result.fileUrl)}')"
              ${!result.fileUrl ? 'disabled' : ''}>
              <i class="bi bi-plus-circle"></i> Add
            </button>
          </td>
        </tr>
      `;
    });
    
    html += `
          </tbody>
        </table>
      </div>
      <div class="text-muted small mt-2">
        Found ${results.length} results
      </div>
    `;
    
    document.getElementById('qbitResults').innerHTML = html;
  })
  .catch(error => {
    document.getElementById('qbitLoading').style.display = 'none';
    document.getElementById('qbitResults').innerHTML = `
      <div class="alert alert-danger">
        <i class="bi bi-exclamation-triangle"></i> Network error: ${error.message}
      </div>
    `;
  });
}

function addTorrent(url) {
  if (!url || !confirm('Add this torrent to qBittorrent?')) return;
  
  fetch('/api/qbittorrent/add', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({ url: url })
  })
  .then(response => response.json())
  .then(data => {
    if (data.success) {
      alert('✅ Torrent added successfully!');
    } else {
      alert('❌ Error: ' + (data.error || 'Failed to add torrent'));
    }
  })
  .catch(error => {
    alert('❌ Network error: ' + error.message);
  });
}

function formatBytes(bytes) {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i];
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function sanitizeBio(text) {
  // Escape all HTML, then restore safe <br> tags (literal or escaped variants) and newlines.
  return escapeHtml(text)
    .replace(/&lt;br\s*\/?&gt;/gi, '<br>')
    .replace(/\n/g, '<br>');
}

function escapeJsString(str) {
  if (!str) return '';
  return str.replace(/\\/g, '\\\\')
            .replace(/'/g, "\\'")
            .replace(/"/g, '\\"')
            .replace(/\n/g, '\\n')
            .replace(/\r/g, '\\r');
}

function formatDuration(rawValue) {
  if (rawValue == null || rawValue === '') return 'N/A';

  let n = Number(rawValue);
  if (!Number.isFinite(n) || n <= 0) return 'N/A';

  let seconds;
  if (n >= 100000000) {
    seconds = n / 1000000;
  } else if (n > 10000) {
    seconds = n / 1000;
  } else {
    seconds = n;
  }

  seconds = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(seconds / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;

  if (hours > 0) {
    return `${hours}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
  }
  return `${mins}:${secs.toString().padStart(2, '0')}`;
}

// Allow Enter key to trigger search
document.addEventListener('DOMContentLoaded', function() {
  const qbitSearchInput = document.getElementById('qbitSearchInput');
  if (qbitSearchInput) {
    qbitSearchInput.addEventListener('keypress', function(e) {
      if (e.key === 'Enter') {
        performQbitSearch();
      }
    });
  }
  
  const slskdSearchInput = document.getElementById('slskdSearchInput');
  if (slskdSearchInput) {
    slskdSearchInput.addEventListener('keypress', function(e) {
      if (e.key === 'Enter') {
        performSlskdSearch();
      }
    });
  }
});

function importMissingRelease(artistName, releaseId, releaseTitle) {
  if (!confirm(`Import all tracks from "${releaseTitle}"?`)) return;
  
  const btn = event.target.closest('button');
  const originalContent = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
  
  fetch('/api/artist/import-release', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      artist: artistName,
      release_id: releaseId,
      title: releaseTitle
    })
  })
  .then(response => response.json())
  .then(data => {
    if (data.success) {
      alert(`✅ ${data.message}`);
      // Refresh the page to show imported album
      setTimeout(() => {
        location.reload();
      }, 1000);
    } else {
      alert('❌ Error: ' + (data.error || 'Failed to import'));
      btn.disabled = false;
      btn.innerHTML = originalContent;
    }
  })
  .catch(error => {
    alert('❌ Network error: ' + error.message);
    btn.disabled = false;
    btn.innerHTML = originalContent;
  });
}

/**
 * Check MusicBrainz for missing releases and inject them inline into the
 * appropriate album sections.
 *
 * @param {string} artistName - Artist to check.
 * @param {boolean} [silent=false] - When true, suppress success/error alert
 *   popups (used for background auto-refresh on page load).
 */
function checkMissingReleases(artistName, silent = false, background = false) {
  const triggerBtn = window.event?.target?.closest('button');
  const originalBtnHtml = triggerBtn ? triggerBtn.innerHTML : null;
  if (triggerBtn) {
    triggerBtn.disabled = true;
    triggerBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Checking...';
  }

  const safeForDomId = (value) => String(value || '').replace(/\s+/g, '_').replace(/[^\w\-]/g, '_');
  const categoryToSection = {
    album: 'albums',
    live_album: 'live-albums',
    remix_album: 'remix-albums',
    ep: 'eps',
    single: 'singles',
    compilation: 'compilations'
  };

  const getMissingCategory = (item) => {
    const categoryRaw = String(item.category || item.primary_type || 'album').toLowerCase();
    if (categoryRaw.includes('compilation')) return 'compilation';
    if (categoryRaw.includes('live')) return 'live_album';
    if (categoryRaw.includes('remix')) return 'remix_album';
    if (categoryRaw.includes('single')) return 'single';
    if (categoryRaw.includes('ep')) return 'ep';
    // Fallback: inspect the title for live/unplugged/remix keywords even when
    // the stored category is the generic "album" value (e.g. secondary type
    // missing from MusicBrainz at scan time).
    const titleLower = String(item.title || '').toLowerCase();
    if (titleLower.includes('live') || titleLower.includes('unplugged')) return 'live_album';
    if (titleLower.includes('remix')) return 'remix_album';
    return 'album';
  };

  const rowExists = (tbody, albumTitle) => {
    const wanted = String(albumTitle || '').trim().toLowerCase();
    return Array.from(tbody.querySelectorAll('tr.album-row')).some(row =>
      String(row.getAttribute('data-album') || '').trim().toLowerCase() === wanted
    );
  };

  const sortTbodyByYear = (tbody) => {
    const albumRows = Array.from(tbody.querySelectorAll('tr.album-row'));
    const rowPairs = albumRows.map((row) => {
      const next = row.nextElementSibling;
      const tracklistRow = (next && next.classList.contains('tracklist-row')) ? next : null;
      return { row, tracklistRow };
    });

    rowPairs.sort((a, b) => {
      const yearA = parseInt(a.row.getAttribute('data-year') || '0', 10);
      const yearB = parseInt(b.row.getAttribute('data-year') || '0', 10);
      return yearB - yearA;
    });

    tbody.innerHTML = '';
    rowPairs.forEach(({ row, tracklistRow }) => {
      tbody.appendChild(row);
      if (tracklistRow) tbody.appendChild(tracklistRow);
    });
  };

  let missingReleasesUrl = '/api/artist/missing-releases?artist=' + encodeURIComponent(artistName);
  if (background) {
    missingReleasesUrl += '&background=1';
  }

  fetch(missingReleasesUrl)
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        throw new Error(data.error);
      }
      const missing = data.missing || [];
      // Remove previously injected live rows before re-inserting.
      document.querySelectorAll('tr[data-source="live-missing"]').forEach(row => row.remove());

      let addedCount = 0;
      missing.forEach(item => {
        const category = getMissingCategory(item);
        const sectionKey = categoryToSection[category];
        const tbody = document.querySelector(`#${sectionKey}-section .${sectionKey}-tbody`);
        if (!tbody) return;
        if (rowExists(tbody, item.title)) return;

        const year = (item.first_release_date || '').slice(0, 4) || '????';
        const fallbackArt = '/api/album-art-placeholder';
        const artUrl = item.cover_art_url || fallbackArt;
        const safeArtist = safeForDomId(artistName);
        const safeAlbum = safeForDomId(item.title);
        const rowId = `tracklist-${safeArtist}-${safeAlbum}`;
        const contentId = `tracklist-content-${safeArtist}-${safeAlbum}`;
        
        const artistEnc = encodeURIComponent(JSON.stringify(artistName || "")).replace(/'/g, '%27');
        const titleEnc = encodeURIComponent(JSON.stringify(item.title || "")).replace(/'/g, '%27');
        const releaseIdEnc = encodeURIComponent(JSON.stringify(item.id || "")).replace(/'/g, '%27');

        const row = document.createElement('tr');
        row.className = 'album-row';
        row.setAttribute('data-year', year === '????' ? '0' : year);
        row.setAttribute('data-status', 'missing');
        row.setAttribute('data-source', 'live-missing');
        row.setAttribute('data-album', item.title || '');
        
        row.innerHTML = `
          <td class="text-center" style="width: 100px; padding: 0.5rem;">
            <img src="${artUrl}" 
                 alt="${escapeHtml(item.title)}" 
                 class="img-thumbnail"
                 style="width: 80px; height: 80px; object-fit: cover; border: 1px solid var(--border-color); background-color: #2a2a2a;"
                 onerror="this.src='${fallbackArt}'">
          </td>
          <td>${escapeHtml(item.title)}</td>
          <td class="text-center">
            ${year}
          </td>
          <td class="text-center">
            <span class="badge bg-warning text-dark">Missing</span>
          </td>
          <td class="text-center">—</td>
          <td class="text-center"><span class="text-muted">—</span></td>
          <td class="text-center">
            <div class="btn-group btn-group-sm" role="group">
              <button class="btn btn-outline-info" onclick="toggleTracklistFromEncoded('${artistEnc}', '${titleEnc}')" title="Show tracklist" id="toggle-btn-${safeArtist}-${safeAlbum}">
                <i class="bi bi-list-ul"></i>
              </button>
              <button class="btn btn-outline-success" onclick="importReleaseFromEncoded('${artistEnc}', '${releaseIdEnc}', '${titleEnc}')" title="Import this release">
                <i class="bi bi-download"></i>
              </button>
              <button class="btn btn-outline-secondary" onclick="searchMusicBrainzReleaseFromEncoded(null, '${artistEnc}', '${titleEnc}')" title="Search MusicBrainz">
                <i class="bi bi-search"></i>
              </button>
            </div>
          </td>
        `;

        const tracklistRow = document.createElement('tr');
        tracklistRow.className = 'tracklist-row';
        tracklistRow.id = rowId;
        tracklistRow.setAttribute('data-source', 'live-missing');
        tracklistRow.style.display = 'none';
        tracklistRow.innerHTML = `
          <td colspan="7" style="padding: 1rem;">
            <div class="tracklist-container">
              <button class="btn btn-sm btn-outline-info me-2 btn-tracklist-load" onclick="loadTracklistFromEncoded('${artistEnc}', '${titleEnc}', this, '${releaseIdEnc}')">
                <span class="spinner-border spinner-border-sm me-2" style="display:none;"></span>
                <i class="bi bi-music-note-list"></i> Load Tracklist from MusicBrainz
              </button>
              <div id="${contentId}" class="mt-3"></div>
            </div>
          </td>
        `;

        tbody.appendChild(row);
        tbody.appendChild(tracklistRow);
        addedCount += 1;
      });

      ['albums', 'live-albums', 'remix-albums', 'eps', 'singles', 'compilations'].forEach(sectionKey => {
        const tbody = document.querySelector(`#${sectionKey}-section .${sectionKey}-tbody`);
        if (tbody) sortTbodyByYear(tbody);
      });

      initializeMissingToggle();
      if (!silent) {
        alert(addedCount > 0
          ? `✅ Added ${addedCount} missing release(s) inline from MusicBrainz`
          : '✅ No new missing releases found.');
      }
    })
    .catch(err => {
      if (!silent) {
        alert('❌ Error checking missing releases: ' + err.message);
      }
    })
    .finally(() => {
      if (triggerBtn) {
        triggerBtn.disabled = false;
        triggerBtn.innerHTML = originalBtnHtml;
      }
    });
}

// Import a missing release into the database
function importRelease(artist, releaseId, title) {
  if (!confirm(`Import "${title}" by ${artist}?\n\nThis will fetch the full tracklist from MusicBrainz and add it to your library.`)) {
    return;
  }
  
  const btn = window.event ? window.event.target.closest('button') : document.activeElement.closest('button');
  const originalHtml = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
  
  fetch('/api/artist/import-release', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      artist: artist,
      release_id: releaseId,
      title: title
    })
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) {
      alert(`Error importing release: ${data.error}`);
      btn.disabled = false;
      btn.innerHTML = originalHtml;
    } else {
      alert(`Success! Imported ${data.tracks_imported} tracks from "${title}"`);
      // Reload the page to show the newly imported album
      window.location.reload();
    }
  })
  .catch(err => {
    alert(`Network error: ${err.message}`);
    btn.disabled = false;
    btn.innerHTML = originalHtml;
  });
}

function decodeInlineArtistArg(value, fallback = '') {
  try {
    const decoded = decodeURIComponent(value);
    try {
      return JSON.parse(decoded);
    } catch (_jsonErr) {
      return decoded || fallback;
    }
  } catch (error) {
    return fallback;
  }
}

function toggleTracklistFromEncoded(artistEnc, albumEnc) {
  return toggleTracklist(
    decodeInlineArtistArg(artistEnc, ''),
    decodeInlineArtistArg(albumEnc, '')
  );
}

function importReleaseFromEncoded(artistEnc, releaseIdEnc, titleEnc) {
  return importRelease(
    decodeInlineArtistArg(artistEnc, ''),
    decodeInlineArtistArg(releaseIdEnc, ''),
    decodeInlineArtistArg(titleEnc, '')
  );
}

function searchMusicBrainzReleaseFromEncoded(event, artistEnc, albumEnc) {
  return searchMusicBrainzRelease(
    event,
    decodeInlineArtistArg(artistEnc, ''),
    decodeInlineArtistArg(albumEnc, '')
  );
}

function loadTracklistFromEncoded(artistEnc, albumEnc, buttonEl, releaseIdEnc) {
  return loadTracklist(
    decodeInlineArtistArg(artistEnc, ''),
    decodeInlineArtistArg(albumEnc, ''),
    buttonEl,
    decodeInlineArtistArg(releaseIdEnc, '')
  );
}

async function searchMusicBrainzRelease(event, artist, album) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }

  // Artist-only lookups must use the artist releases flow to avoid broad
  // text-search results from similarly named artists.
  if (!album || !String(album).trim()) {
    searchMusicBrainzForAllReleases(artist);
    return;
  }

  const modalEl = document.getElementById('musicBrainzModal');
  const statusEl = document.getElementById('mbSearchStatus');
  const errorEl = document.getElementById('mbSearchError');
  const resultsEl = document.getElementById('mbSearchResults');
  const infoEl = document.getElementById('mbSearchInfo');
  const infoArtistEl = document.getElementById('mbSearchArtist');
  const infoAlbumEl = document.getElementById('mbSearchAlbum');

  if (!modalEl || !statusEl || !errorEl || !resultsEl) {
    alert('Search UI not available on this page.');
    return;
  }

  const hasBootstrapModal = !!(window.bootstrap && window.bootstrap.Modal);
  if (hasBootstrapModal) {
    const modal = new bootstrap.Modal(modalEl);
    modal.show();
  } else {
    // Fallback for environments where Bootstrap JS is not loaded.
    modalEl.style.display = 'block';
    modalEl.classList.add('show');
    modalEl.removeAttribute('aria-hidden');
    document.body.classList.add('modal-open');
  }

  if (infoEl && infoArtistEl && infoAlbumEl) {
    infoArtistEl.textContent = artist;
    infoAlbumEl.textContent = album;
    infoEl.style.display = 'block';
  }

  statusEl.style.display = 'block';
  statusEl.innerHTML = '<div class="spinner-border spinner-border-sm me-2"></div>Searching MusicBrainz...';
  errorEl.style.display = 'none';
  const retryEl = document.getElementById('mbSearchRetry');
  if (retryEl) retryEl.style.display = 'none';
  resultsEl.innerHTML = '';

  const _showRetry = (a, b) => {
    if (!retryEl) return;
    const artistInput = document.getElementById('mbRetryArtist');
    const albumInput  = document.getElementById('mbRetryAlbum');
    if (artistInput) artistInput.value = a || '';
    if (albumInput)  albumInput.value  = b || '';
    retryEl.style.display = 'block';
  };

  const parseJsonResponse = async (resp, sourceName) => {
    const contentType = (resp.headers.get('content-type') || '').toLowerCase();
    if (!contentType.includes('application/json')) {
      const raw = await resp.text();
      const htmlResponse = raw && raw.trim().startsWith('<');
      const hint = htmlResponse
        ? 'Received HTML instead of JSON (possible auth/session redirect or server error).'
        : `Unexpected response format from ${sourceName}.`;
      throw new Error(`${hint} HTTP ${resp.status}`);
    }

    const parsed = await resp.json();
    if (!resp.ok) {
      throw new Error(parsed.error || parsed.message || `${sourceName} request failed (HTTP ${resp.status})`);
    }
    return parsed;
  };

  try {
    const response = await fetch('/api/upcoming-releases/search-musicbrainz', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ artist, album })
    });
    const data = await parseJsonResponse(response, 'MusicBrainz');

    const isArtistOnlySearch = !album || !String(album).trim();

    if (data.success && data.results && data.results.length >= (isArtistOnlySearch ? 1 : 3)) {
      statusEl.style.display = 'none';
      displayMusicBrainzResults(data.results);
      return;
    }

    if (isArtistOnlySearch) {
      statusEl.style.display = 'none';
      errorEl.textContent = 'No releases found on MusicBrainz for this artist';
      errorEl.style.display = 'block';
      _showRetry(artist, album);
      return;
    }

    statusEl.innerHTML = '<div class="spinner-border spinner-border-sm me-2"></div>Searching Discogs fallback...';

    // Discogs is optional — if it is not configured or returns an error, treat it as
    // "no Discogs results" rather than surfacing the configuration error to the user.
    let discogsData = { success: false, results: [] };
    try {
      const discogsResponse = await fetch('/api/upcoming-releases/search-discogs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ artist, album })
      });
      const discogsJson = await discogsResponse.json().catch(() => ({}));
      if (discogsResponse.ok && discogsJson.success) {
        discogsData = discogsJson;
      } else {
        console.warn('Discogs fallback skipped:', discogsJson.error || `HTTP ${discogsResponse.status}`);
      }
    } catch (discogsErr) {
      console.warn('Discogs fallback fetch failed:', discogsErr.message);
    }

    statusEl.style.display = 'none';

    const allResults = [];
    if (Array.isArray(data.results)) allResults.push(...data.results);
    if (Array.isArray(discogsData.results)) allResults.push(...discogsData.results);

    if (allResults.length === 0) {
      errorEl.textContent = 'No releases found on MusicBrainz or Discogs';
      errorEl.style.display = 'block';
      _showRetry(artist, album);
      return;
    }

    displayMusicBrainzResults(allResults);
  } catch (error) {
    statusEl.style.display = 'none';
    errorEl.textContent = 'Error searching: ' + error.message;
    errorEl.style.display = 'block';
    _showRetry(artist, album);
  }
}

function mbRetrySearch() {
  const artist = (document.getElementById('mbRetryArtist')?.value || '').trim();
  const album  = (document.getElementById('mbRetryAlbum')?.value  || '').trim();
  searchMusicBrainzRelease(null, artist, album);
}

function displayMusicBrainzResults(results) {
  const container = document.getElementById('mbSearchResults');
  
  // Global storage for release data to avoid JSON escaping in HTML attributes
  if (!window.mbReleaseData) {
    window.mbReleaseData = {};
  }
  
  let html = '<div class="accordion" id="mbResultsAccordion">';
  
  results.forEach((release, index) => {
    const releaseId = `mbRelease${index}`;
    const dataKey = `release_${Date.now()}_${index}`;
    
    // Store release data in global object for safe access
    window.mbReleaseData[dataKey] = {
      artist: release.artist,
      album: release.title,
      tracks: release.tracks,
      year: release.date || release.first_release_date || release.year || null,
      release_id: release.release_id || release.release_group_id || null,
      source: release.source || 'musicbrainz'
    };
    
    // Handle both MusicBrainz and Discogs formats
    const source = release.source || 'musicbrainz';
    const sourceBadge = source === 'discogs' ? 
      '<span class="badge bg-info ms-2">Discogs</span>' : 
      '<span class="badge bg-primary ms-2">MusicBrainz</span>';
    
    // Format tracks based on source
    const tracksHtml = release.tracks.map((track, trackIndex) => {
      // MusicBrainz uses 'length' in milliseconds, Discogs uses 'duration' as string
      let duration = 'N/A';
      if (track.length != null && track.length !== '') {
        duration = formatDuration(track.length);
      } else if (track.duration != null && track.duration !== '') {
        duration = track.duration;
      }
      
      return `
        <tr>
          <td style="width: 44px;" class="text-center">
            <input type="checkbox" class="form-check-input mb-track-select" data-release-key="${dataKey}" data-track-index="${trackIndex}">
          </td>
          <td>${escapeHtml(track.position)}</td>
          <td>${escapeHtml(track.title)}</td>
          <td>${duration}</td>
          <td style="width: 120px;" class="text-center">
            <button class="btn btn-sm btn-outline-success mb-download-track" data-release-key="${dataKey}" data-track-index="${trackIndex}">
              <i class="bi bi-download"></i> Download
            </button>
          </td>
        </tr>
      `;
    }).join('');
    
    // Build release info
    let releaseInfo = [];
    if (release.type) {
      releaseInfo.push(release.type);
    } else if (release.formats && Array.isArray(release.formats) && release.formats.length > 0) {
      const formatStr = release.formats.join(', ');
      releaseInfo.push(formatStr);
    } else {
      releaseInfo.push('Album');
    }
    
    if (release.date) releaseInfo.push(release.date);
    else if (release.first_release_date) releaseInfo.push(release.first_release_date);
    else if (release.year) releaseInfo.push(release.year);
    else releaseInfo.push('Unknown date');
    
    releaseInfo.push(`${release.track_count} tracks`);
    
    html += `
      <div class="accordion-item">
        <h2 class="accordion-header" id="heading${releaseId}">
          <button class="accordion-button ${index === 0 ? '' : 'collapsed'}" type="button" 
            data-bs-toggle="collapse" data-bs-target="#${releaseId}" 
            aria-expanded="${index === 0 ? 'true' : 'false'}" aria-controls="${releaseId}">
            <div class="w-100">
              <strong>${escapeHtml(release.title)}</strong>${sourceBadge}
              <small class="text-muted ms-2">
                ${releaseInfo.join(' · ')}
              </small>
            </div>
          </button>
        </h2>
        <div id="${releaseId}" class="accordion-collapse collapse ${index === 0 ? 'show' : ''}" 
          aria-labelledby="heading${releaseId}" data-bs-parent="#mbResultsAccordion">
          <div class="accordion-body">
            <div class="mb-3 d-flex flex-wrap align-items-center gap-2">
              <button class="btn btn-success mb-download-release" 
                data-release-key="${dataKey}">
                <i class="bi bi-download"></i> Download All Tracks (${release.track_count})
              </button>
              <button class="btn btn-outline-success mb-download-selected" data-release-key="${dataKey}" disabled>
                <i class="bi bi-check2-square"></i> Download Selected (0)
              </button>
              <div class="form-check mb-0 ms-md-2">
                <input class="form-check-input mb-select-all" type="checkbox" id="mbSelectAll_${releaseId}" data-release-key="${dataKey}">
                <label class="form-check-label" for="mbSelectAll_${releaseId}">Select All</label>
              </div>
            </div>
            <table class="table table-sm table-hover table-striped table-dark mb-0">
              <thead>
                <tr>
                  <th style="width: 44px;" class="text-center"></th>
                  <th style="width: 60px;">#</th>
                  <th>Title</th>
                  <th style="width: 100px;">Duration</th>
                  <th style="width: 120px;" class="text-center">Action</th>
                </tr>
              </thead>
              <tbody>
                ${tracksHtml}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    `;
  });
  
  html += '</div>';
  container.innerHTML = html;
  
  // Attach event listeners to download buttons
  container.querySelectorAll('.mb-download-release').forEach(button => {
    button.addEventListener('click', async function() {
      const dataKey = this.dataset.releaseKey;
      const releaseData = window.mbReleaseData[dataKey];

      if (!releaseData) {
        alert('Error: Release data not found');
        return;
      }

      const btn = this;
      const originalHtml = btn.innerHTML;
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span> Queuing…';
      try {
        await downloadMusicBrainzRelease(releaseData.artist, releaseData.album, releaseData.tracks, releaseData.year, releaseData.release_id, releaseData.source);
      } finally {
        btn.disabled = false;
        btn.innerHTML = originalHtml;
      }
    });
  });

  container.querySelectorAll('.mb-download-track').forEach(button => {
    button.addEventListener('click', async function() {
      const dataKey = this.dataset.releaseKey;
      const trackIndex = Number(this.dataset.trackIndex);
      const releaseData = window.mbReleaseData[dataKey];

      if (!releaseData || !Array.isArray(releaseData.tracks) || !releaseData.tracks[trackIndex]) {
        alert('Error: Track data not found');
        return;
      }

      const queued = await downloadMusicBrainzRelease(
        releaseData.artist,
        releaseData.album,
        [releaseData.tracks[trackIndex]],
        releaseData.year,
        releaseData.release_id,
        releaseData.source,
        { closeModal: false, selectionLabel: 'track' }
      );
      if (queued) {
        markMBTrackQueued(container, dataKey, trackIndex);
      }
    });
  });

  container.querySelectorAll('.mb-download-selected').forEach(button => {
    button.addEventListener('click', async function() {
      const dataKey = this.dataset.releaseKey;
      const releaseData = window.mbReleaseData[dataKey];
      const selectedTracks = getSelectedTracksForRelease(dataKey);
      const selectedIndices = Array.from(container.querySelectorAll(`.mb-track-select[data-release-key="${dataKey}"]:checked`))
        .map(input => Number(input.dataset.trackIndex));

      if (!releaseData || selectedTracks.length === 0) {
        alert('Select at least one track first');
        return;
      }

      const queued = await downloadMusicBrainzRelease(
        releaseData.artist,
        releaseData.album,
        selectedTracks,
        releaseData.year,
        releaseData.release_id,
        releaseData.source,
        { closeModal: false, selectionLabel: 'selected tracks' }
      );
      if (queued) {
        selectedIndices.forEach(index => markMBTrackQueued(container, dataKey, index));
      }
    });
  });

  container.querySelectorAll('.mb-select-all').forEach(checkbox => {
    checkbox.addEventListener('change', function() {
      const dataKey = this.dataset.releaseKey;
      const checked = this.checked;
      container.querySelectorAll(`.mb-track-select[data-release-key="${dataKey}"]`).forEach(trackBox => {
        trackBox.checked = checked;
      });
      updateMBSelectionUI(container, dataKey);
    });
  });

  container.querySelectorAll('.mb-track-select').forEach(checkbox => {
    checkbox.addEventListener('change', function() {
      const dataKey = this.dataset.releaseKey;
      updateMBSelectionUI(container, dataKey);
    });
  });
}

function getSelectedTracksForRelease(dataKey) {
  const releaseData = window.mbReleaseData && window.mbReleaseData[dataKey];
  if (!releaseData || !Array.isArray(releaseData.tracks)) {
    return [];
  }

  const selectedIndices = Array.from(document.querySelectorAll(`.mb-track-select[data-release-key="${dataKey}"]:checked`))
    .map(input => Number(input.dataset.trackIndex))
    .filter(index => Number.isInteger(index) && index >= 0 && index < releaseData.tracks.length);

  return selectedIndices.map(index => releaseData.tracks[index]);
}

function updateMBSelectionUI(container, dataKey) {
  const allTrackBoxes = container.querySelectorAll(`.mb-track-select[data-release-key="${dataKey}"]`);
  const selectedTrackBoxes = container.querySelectorAll(`.mb-track-select[data-release-key="${dataKey}"]:checked`);
  const selectedCount = selectedTrackBoxes.length;
  const totalCount = allTrackBoxes.length;

  const selectedBtn = container.querySelector(`.mb-download-selected[data-release-key="${dataKey}"]`);
  if (selectedBtn) {
    selectedBtn.disabled = selectedCount === 0;
    selectedBtn.innerHTML = `<i class="bi bi-check2-square"></i> Download Selected (${selectedCount})`;
  }

  const selectAll = container.querySelector(`.mb-select-all[data-release-key="${dataKey}"]`);
  if (selectAll) {
    selectAll.checked = totalCount > 0 && selectedCount === totalCount;
    selectAll.indeterminate = selectedCount > 0 && selectedCount < totalCount;
  }
}

function markMBTrackQueued(container, dataKey, trackIndex) {
  const rowCheckbox = container.querySelector(`.mb-track-select[data-release-key="${dataKey}"][data-track-index="${trackIndex}"]`);
  if (rowCheckbox) {
    rowCheckbox.checked = false;
    rowCheckbox.disabled = true;
  }

  const rowButton = container.querySelector(`.mb-download-track[data-release-key="${dataKey}"][data-track-index="${trackIndex}"]`);
  if (rowButton) {
    rowButton.disabled = true;
    rowButton.classList.remove('btn-outline-success');
    rowButton.classList.add('btn-success');
    rowButton.innerHTML = '<i class="bi bi-check2"></i> Queued';
  }

  updateMBSelectionUI(container, dataKey);
}

async function downloadMusicBrainzRelease(artist, album, tracks, year, release_id, source, options = {}) {
  if (!tracks || tracks.length === 0) {
    alert('No tracks to download');
    return false;
  }

  // Keep modal open by default so users can queue multiple albums without re-running the search.
  const closeModal = options.closeModal === true;
  const selectionLabel = options.selectionLabel || null;
  
  try {
    // Extract year from date string if needed (e.g., "2023-04-15" -> "2023")
    let releaseYear = null;
    if (year) {
      // Handle both string and number types
      const yearStr = String(year);
      releaseYear = yearStr.substring(0, 4);
    }
    
    // Batch add all tracks in a single API call with MusicBrainz/Discogs metadata.
    // For "Various Artists" compilations each track can carry its own artist field
    // (populated by the MusicBrainz search endpoint).  When a per-track artist is
    // present it is used as the queue item artist so files are tagged and named
    // correctly, while album_artist always stores the release-level artist (e.g.
    // "Various Artists") for correct library folder organisation.
    const trackItems = tracks.map(track => ({
      artist: track.artist || artist,
      title: track.title,
      album: album,
      source: 'soulseek',
      priority: 5,
      track_number: track.position || null,
      disc_number: track.disc_number || null,
      album_artist: artist,
      year: releaseYear,
      release_id: release_id,
      // release_mbid fills a separate column used for MB-specific duplicate
      // detection and cross-source overwrite merge in add_to_queue.
      release_mbid: release_id || null,
      release_source: source || 'musicbrainz',
      recording_mbid: track.recording_mbid || null,
      duration: track.length || null,
    }));
    
    // Send batch request with import_group based on artist+album
    // This tags all tracks from this specific album together
    // Once all downloads complete, they can be organized together using "Organize All" button
    const import_group = `${artist}_${album}`.replace(/\s+/g, '_').substring(0, 100);
    
    const response = await fetch('/api/queue/add-batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        items: trackItems,
        import_group: import_group,  // Group by artist + album
        import_type: 'album'  // Mark as album import
      })
    });
    
    const data = await response.json();
    
    // Close modal for full-album downloads only
    if (closeModal) {
      const modalEl = document.getElementById('musicBrainzModal');
      const modal = bootstrap.Modal.getInstance(modalEl);
      if (modal) {
        modal.hide();
      }
    }
    
    // Show result message
    if (data.success) {
      const added = data.added || 0;
      const skipped = data.skipped || 0;
      const failed = data.failed || 0;
      const skippedTracks = data.skipped_tracks || [];
      const failedTracks = data.failed_tracks || [];
      const importGroup = data.import_group;
      const importType = data.import_type || 'album';
      
      const label = selectionLabel ? ` ${selectionLabel}` : ' tracks';
      let message = `✅ Added ${added}${label} from "${album}" to download queue`;
      if (skipped > 0) {
        message += `\nℹ️ Skipped ${skipped} already queued tracks`;
        if (skippedTracks.length > 0) {
          const skippedList = skippedTracks.slice(0, 5).join(', ');
          message += `:\n${skippedList}${skippedTracks.length > 5 ? '...' : ''}`;
        }
      }
      if (failed > 0) {
        message += `\n⚠️ Failed to add ${failed} tracks`;
        if (failedTracks.length > 0) {
          const failedList = failedTracks.slice(0, 5).join(', ');
          message += `:\n${failedList}${failedTracks.length > 5 ? '...' : ''}`;
        }
      }
      
      // Inform user about grouping and organization
      if (added > 0 && importGroup) {
        message += `\n\n📦 All ${importType} tracks are grouped as: "${album}"`;
        message += `\nOnce downloads complete, use "Organize All" in the Completed section to move them to /music`;
      }
      
      alert(message);
      
      // Refresh queue status if the function exists (from downloads_monitor context)
      if (typeof loadQueueStatus === 'function') {
        await loadQueueStatus();
      }
      
      // Try to trigger refresh on monitor page if it's open in another tab
      try {
        // Use localStorage to notify other tabs/windows to refresh
        const timestamp = Date.now();
        localStorage.setItem('popularr_queue_updated', timestamp.toString());
      } catch (e) {
        console.warn('Could not update localStorage:', e);
      }
      return true;
    } else {
      alert('❌ Error: ' + (data.error || 'Failed to add tracks to queue'));
      return false;
    }
    
  } catch (error) {
    console.error('Error downloading release:', error);
    alert('❌ Error: ' + error.message);
    return false;
  }
}


// Load artist bio on page load
document.addEventListener('DOMContentLoaded', () => {
  const artistName = {{ artist_name|tojson }};
  const bioContainer = document.getElementById('artistBio');
  const hasInitialBio = bioContainer && bioContainer.dataset && bioContainer.dataset.hasInitialBio === '1';
  if (!hasInitialBio) {
    loadArtistBio(artistName);
  }
  loadSinglesCount(artistName);
});

function loadArtistBio(artistName) {
  const bioContainer = document.getElementById('artistBio');
  if (!bioContainer) return;

  fetch(`/api/artist/bio?name=${encodeURIComponent(artistName)}`)
    .then(r => r.json())
    .then(data => {
      if (data.bio && data.bio.length > 0) {
        bioContainer.innerHTML = `<p>${sanitizeBio(data.bio)}</p>`;
        if (data.source) {
          bioContainer.innerHTML += `<p class="small text-muted mt-2"><em>Source: ${escapeHtml(data.source)}</em></p>`;
        }
      } else {
        bioContainer.innerHTML = '<p class="text-muted"><em>No biography available for this artist.</em></p>';
      }
    })
    .catch(err => {
      bioContainer.innerHTML = '<p class="text-muted"><em>Unable to load artist biography.</em></p>';
    });
}

function loadSinglesCount(artistName) {
  fetch(`/api/artist/singles-count?name=${encodeURIComponent(artistName)}`)
    .then(r => r.json())
    .then(data => {
      const badge = document.getElementById('singlesCount');
      if (badge && data.count !== undefined) {
        badge.textContent = data.count;
      }
    })
    .catch(err => console.error('Failed to load singles count:', err));
}

function showArtistSingles(artistName) {
  window.location.href = `/artist/${encodeURIComponent(artistName)}/singles`;
}

function createEssentialPlaylist(artistName) {
  if (!confirm(`Create Essential Playlist for ${artistName}?\\nThis will use single detection to pick the best tracks.`)) return;

  const btn = event.target.closest('button');
  const originalContent = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Creating...';

  fetch('/api/artist/create-essential-playlist', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ artist: artistName })
  })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        alert(`✅ ${data.message}\\nPlaylist: ${data.playlist_name}`);
        if (data.navidrome_url) {
          if (confirm('Open playlist in Navidrome?')) {
            window.open(data.navidrome_url, '_blank');
          }
        }
      } else {
        alert('❌ Error: ' + (data.error || 'Failed to create playlist'));
      }
      btn.disabled = false;
      btn.innerHTML = originalContent;
    })
    .catch(err => {
      alert('❌ Network error: ' + err.message);
      btn.disabled = false;
      btn.innerHTML = originalContent;
    });
}

function openArtistImageModal(artistName) {
  // For now, create a simple modal for image search
  const modalHtml = `
    <div class="modal fade" id="artistImageModal" tabindex="-1">
      <div class="modal-dialog modal-lg">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title"><i class="bi bi-image"></i> Change Artist Image</h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body">
            <div class="mb-3">
              <label for="manualImageUrl" class="form-label">Manual Image URL</label>
              <input type="text" class="form-control" id="manualImageUrl" placeholder="https://example.com/image.jpg">
            </div>
            <div class="d-flex gap-2 mb-4 flex-wrap">
              <button class="btn btn-primary" onclick="applyManualImage('${escapeJsString(artistName)}')">
                <i class="bi bi-check"></i> Apply URL
              </button>
              <button class="btn btn-secondary" onclick="searchArtistImages('${escapeJsString(artistName)}', 'musicbrainz')">
                <i class="bi bi-search"></i> MusicBrainz
              </button>
              <button class="btn btn-secondary" onclick="searchArtistImages('${escapeJsString(artistName)}', 'discogs')">
                <i class="bi bi-disc"></i> Discogs
              </button>
              <button class="btn btn-secondary" onclick="searchArtistImages('${escapeJsString(artistName)}', 'applemusic')">
                <i class="bi bi-apple"></i> Apple Music
              </button>
            </div>
            <div id="artistImageResults"></div>
          </div>
        </div>
      </div>
    </div>
  `;
  
  // Remove existing modal if any
  const existingModal = document.getElementById('artistImageModal');
  if (existingModal) existingModal.remove();
  
  document.body.insertAdjacentHTML('beforeend', modalHtml);
  const modal = new bootstrap.Modal(document.getElementById('artistImageModal'));
  modal.show();
}

function applyManualImage(artistName) {
  const url = document.getElementById('manualImageUrl').value;
  if (!url) {
    alert('Please enter an image URL');
    return;
  }
  applyArtistImage(artistName, url);
}

function searchArtistImages(artistName, source) {
  const resultsDiv = document.getElementById('artistImageResults');
  resultsDiv.innerHTML = '<div class="text-center"><span class="spinner-border"></span> Searching...</div>';

  fetch(`/api/artist/search-images?name=${encodeURIComponent(artistName)}&source=${source}`)
    .then(r => r.json())
    .then(data => {
      if (data.error || !data.images || data.images.length === 0) {
        resultsDiv.innerHTML = '<div class="alert alert-info">No images found</div>';
        return;
      }

      let html = '<div class="row g-3">';
      data.images.forEach(img => {
        html += `
          <div class="col-6 col-md-4">
            <div class="card">
              <img src="${escapeHtml(img.url)}" class="card-img-top" style="height: 200px; object-fit: cover;">
              <div class="card-body p-2">
                <button class="btn btn-sm btn-primary w-100" onclick="applyArtistImage('${escapeJsString(artistName)}', '${escapeJsString(img.url)}')">
                  <i class="bi bi-check"></i> Use This
                </button>
              </div>
            </div>
          </div>
        `;
      });
      html += '</div>';
      resultsDiv.innerHTML = html;
    })
    .catch(err => {
      resultsDiv.innerHTML = '<div class="alert alert-danger">Error: ' + err.message + '</div>';
    });
}

function applyArtistImage(artistName, imageUrl) {
  fetch('/api/artist/set-image', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ artist: artistName, image_url: imageUrl })
  })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        document.getElementById('artistImage').src = imageUrl + '?t=' + Date.now();
        bootstrap.Modal.getInstance(document.getElementById('artistImageModal'))?.hide();
        alert('✅ Artist image updated!');
      } else {
        alert('❌ Error: ' + (data.error || 'Failed to update image'));
      }
    })
    .catch(err => alert('❌ Network error: ' + err.message));
}

function openSlskdSearch(query, artistName) {
  // If artistName is provided, search for "artist + query", otherwise just query
  const searchQuery = (artistName ? artistName + ' ' + query : query)
    .replace(/\\u0026/gi, ' ')
    .replace(/&amp;/gi, ' ')
    .replace(/&/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  window.location.href = `/downloads?search=${encodeURIComponent(searchQuery)}`;
}

function fetchArtistGenreRecommendations() {
  const artistName = {{ artist_name|tojson }};
  const btn = document.getElementById('fetchArtistGenresBtn');
  const container = document.getElementById('recommendedArtistGenres');
  const section = document.getElementById('recommendedArtistGenresSection');
  
  // Show the recommendations section
  section.style.display = 'block';
  
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Fetching...';
  container.innerHTML = '<span class="text-muted small">Loading recommendations...</span>';
  
  // Fetch recommendations through backend so MusicBrainz headers are always set server-side.
  fetch(`/api/artist/genre-recommendations?artist=${encodeURIComponent(artistName)}`)
    .then(r => {
      if (!r.ok) {
        throw new Error('Failed to fetch artist recommendations');
      }
      return r.json();
    })
    .then(data => {
      const genres = new Set();

      if (Array.isArray(data.recommendations)) {
        data.recommendations.forEach(genre => {
          if (genre && String(genre).trim()) {
            genres.add(String(genre).trim());
          }
        });
      }
      
      if (genres.size === 0) {
        container.innerHTML = '<span class="text-muted small">No genre recommendations found from MusicBrainz</span>';
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-cloud-download"></i> Get Online Suggestions';
        return;
      }
      
      // Display genres as selectable badges
      let html = '';
      Array.from(genres).sort().forEach(genre => {
        html += `<span class="badge badge-outline-primary artist-genre-badge" 
          onclick="toggleArtistGenreSelection('${escapeHtml(genre)}')" 
          data-artist-genre="${escapeHtml(genre)}"
          style="cursor: pointer; border: 2px solid #0d6efd; background-color: transparent; color: #0d6efd;">
          ${escapeHtml(genre)}
        </span>`;
      });
      container.innerHTML = html;
      
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-cloud-download"></i> Get Online Suggestions';
      document.getElementById('applyArtistGenresBtn').style.display = 'inline-block';
    })
    .catch(error => {
      container.innerHTML = '<span class="text-danger small">Error fetching recommendations</span>';
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-cloud-download"></i> Get Online Suggestions';
    });
}

// Artist genre selection tracking
let selectedArtistGenres = new Set();

function toggleArtistGenreSelection(genre) {
  const badge = document.querySelector(`[data-artist-genre="${escapeHtml(genre)}"]`);
  if (!badge) return;
  
  if (selectedArtistGenres.has(genre)) {
    selectedArtistGenres.delete(genre);
    badge.style.backgroundColor = 'transparent';
    badge.style.color = '#0d6efd';
  } else {
    selectedArtistGenres.add(genre);
    badge.style.backgroundColor = '#0d6efd';
    badge.style.color = '#fff';
  }
}

function applySelectedArtistGenres() {
  if (selectedArtistGenres.size === 0) {
    alert('Please select at least one genre to apply');
    return;
  }
  
  const artistName = {{ artist_name|tojson }};
  const genresArray = Array.from(selectedArtistGenres);
  
  if (!confirm(`Apply genres [${genresArray.join(', ')}] to ALL tracks by ${artistName}?\n\nThis will update the MP3 files for all albums by this artist.`)) {
    return;
  }
  
  const btn = document.getElementById('applyArtistGenresBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Applying...';
  
  fetch('/api/artist/apply-genres', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      artist: artistName,
      genres: genresArray
    })
  })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        alert(`✅ ${data.message || 'Genres applied successfully to all artist tracks'}`);
        // Refresh the page to show updated genres
        location.reload();
      } else {
        alert('❌ Error: ' + (data.error || 'Failed to apply genres'));
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-check-circle"></i> Apply Selected to All Artist Tracks (MP3 Files)';
      }
    })
    .catch(error => {
      alert('❌ Network error: ' + error.message);
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-check-circle"></i> Apply Selected to All Artist Tracks (MP3 Files)';
    });
}

function fetchArtistCountry() {
  const artistName = {{ artist_name|tojson }};
  const btn = document.getElementById('fetchArtistCountryBtn');
  const container = document.getElementById('artistCountryDisplay');
  
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Fetching...';
  container.innerHTML = '<span class="text-muted small">Loading country information from MusicBrainz...</span>';
  
  fetch('/api/artist/country', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ artist_name: artistName })
  })
    .then(r => r.json())
    .then(data => {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-cloud-download"></i> Get from MusicBrainz';
      
      if (data.success && data.country) {
        container.innerHTML = `
          <span class="badge bg-info" style="font-size: 1rem; padding: 0.5rem 1rem;">
            <i class="bi bi-geo-alt"></i> ${escapeHtml(data.country)}
          </span>
          <p class="text-muted small mt-2 mb-0">This can be used as a genre tag for classification</p>
        `;
        // Show a success notification
        alert('✅ ' + data.message);
      } else {
        container.innerHTML = `
          <div class="alert alert-warning mb-0">
            <i class="bi bi-exclamation-triangle"></i> ${escapeHtml(data.error || 'No country information found')}
          </div>
        `;
      }
    })
    .catch(error => {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-cloud-download"></i> Get from MusicBrainz';
      container.innerHTML = `
        <div class="alert alert-danger mb-0">
          <i class="bi bi-x-circle"></i> Error: ${escapeHtml(error.message)}
        </div>
      `;
    });
}

function editArtistCountry() {
  const artistName = {{ artist_name|tojson }};
  // Extract country text by getting text nodes (skipping the icon element)
  const badgeEl = document.querySelector('#artistCountryDisplay .badge');
  const currentCountry = badgeEl ? Array.from(badgeEl.childNodes)
    .filter(node => node.nodeType === Node.TEXT_NODE)
    .map(node => node.textContent.trim())
    .join('') : '';
  
  const modalHtml = `
    <div class="modal fade" id="editCountryModal" tabindex="-1">
      <div class="modal-dialog">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title"><i class="bi bi-pencil"></i> Edit Artist Country/Origin</h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body">
            <div class="mb-3">
              <label for="countryInput" class="form-label">Country/Origin</label>
              <input type="text" class="form-control" id="countryInput" value="${escapeHtml(currentCountry)}" placeholder="e.g., United States, United Kingdom, Japan">
              <div class="form-text">Enter the artist's country or region of origin</div>
            </div>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
            <button type="button" class="btn btn-primary" onclick="saveArtistCountry()">
              <i class="bi bi-save"></i> Save
            </button>
          </div>
        </div>
      </div>
    </div>
  `;
  
  const existingModal = document.getElementById('editCountryModal');
  if (existingModal) existingModal.remove();
  
  document.body.insertAdjacentHTML('beforeend', modalHtml);
  const modal = new bootstrap.Modal(document.getElementById('editCountryModal'));
  modal.show();
}

function saveArtistCountry() {
  const artistName = {{ artist_name|tojson }};
  const country = document.getElementById('countryInput').value.trim();
  
  if (!country) {
    alert('Please enter a country');
    return;
  }
  
  fetch('/api/artist/country/update', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ artist_name: artistName, country: country })
  })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        bootstrap.Modal.getInstance(document.getElementById('editCountryModal'))?.hide();
        
        // Update the display
        const container = document.getElementById('artistCountryDisplay');
        container.innerHTML = `
          <div class="d-flex align-items-center gap-2 mb-2">
            <span class="badge bg-info" style="font-size: 0.95rem; padding: 0.4rem 0.8rem;">
              ${escapeHtml(country)}
            </span>
          </div>
        `;
        alert('✅ Country updated successfully!');
      } else {
        alert('❌ Error: ' + (data.error || 'Failed to update country'));
      }
    })
    .catch(err => alert('❌ Network error: ' + err.message));
}

function applyCountryAsGenre() {
  const artistName = {{ artist_name|tojson }};
  const countryBadge = document.querySelector('#artistCountryDisplay .badge');
  if (!countryBadge) {
    alert('No country information available');
    return;
  }
  
  // Extract country by getting the last text node (after the icon)
  const country = Array.from(countryBadge.childNodes)
    .filter(node => node.nodeType === Node.TEXT_NODE)
    .map(node => node.textContent.trim())
    .join('');
  
  if (!confirm(`Apply "${country}" as a genre tag to all tracks by ${artistName}?`)) {
    return;
  }
  
  fetch('/api/artist/country/apply-as-genre', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ artist_name: artistName })
  })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        alert(`✅ ${data.message}\n\nUpdated ${data.tracks_updated} track(s)`);
      } else {
        alert('❌ Error: ' + (data.error || 'Failed to apply genre'));
      }
    })
    .catch(err => alert('❌ Network error: ' + err.message));
}

// Remove single genre from all artist tracks (uses shared utilities)
function removeGenreFromArtist(genre) {
  const artistName = {{ artist_name|tojson }};
  handleGenreRemoval(artistName, null, [genre], 'artist');
}

// Remove selected genres from all artist tracks (batch removal)
function removeSelectedArtistGenres() {
  const artistName = {{ artist_name|tojson }};
  const selectedGenres = getSelectedGenres('currentArtistGenres');
  
  if (selectedGenres.length === 0) {
    alert('No genres selected');
    return;
  }
  
  handleGenreRemoval(artistName, null, selectedGenres, 'artist');
}

// Toggle apply button visibility when source-tag checkboxes change
document.addEventListener('change', function(e) {
  if (e.target && e.target.classList.contains('artist-source-tag-check')) {
    const pane = e.target.closest('.tab-pane');
    if (!pane) return;
    const applyBtn = pane.querySelector('.artist-apply-source-tags-btn');
    if (!applyBtn) return;
    const anyChecked = pane.querySelectorAll('.artist-source-tag-check:checked').length > 0;
    applyBtn.style.display = anyChecked ? '' : 'none';
  }
});

function applySelectedArtistSourceTags() {
  const artistName = {{ artist_name|tojson }};
  const checked = document.querySelectorAll('.artist-source-tag-check:checked');
  const selected = Array.from(checked).map(cb => cb.value).filter(Boolean);
  if (selected.length === 0) {
    alert('No tags selected');
    return;
  }
  if (!confirm(`Save tags [${selected.join(', ')}] to ALL tracks by ${artistName}?`)) {
    return;
  }
  const btns = document.querySelectorAll('.artist-apply-source-tags-btn');
  btns.forEach(b => { b.disabled = true; b.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Saving…'; });
  fetch('/api/artist/apply-genres', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ artist: artistName, genres: selected })
  })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        alert('✅ ' + (data.message || 'Tags saved to all artist tracks'));
        location.reload();
      } else {
        alert('❌ Error: ' + (data.error || 'Failed to save tags'));
        btns.forEach(b => { b.disabled = false; b.innerHTML = '<i class="bi bi-check-circle"></i> Save Selected to All Artist Tracks'; });
      }
    })
    .catch(err => {
      alert('❌ Network error: ' + err.message);
      btns.forEach(b => { b.disabled = false; b.innerHTML = '<i class="bi bi-check-circle"></i> Save Selected to All Artist Tracks'; });
    });
}

// Toggle tracklist visibility
function toggleTracklist(artist, album) {
  const rowId = `tracklist-${artist.replace(/\s+/g, '_')}-${album.replace(/\s+/g, '_')}`;
  const btnId = `toggle-btn-${artist.replace(/\s+/g, '_')}-${album.replace(/\s+/g, '_')}`;
  const contentId = `tracklist-content-${artist.replace(/\s+/g, '_')}-${album.replace(/\s+/g, '_')}`;
  const row = document.getElementById(rowId);
  const btn = document.getElementById(btnId);
  
  if (row) {
    const isHidden = row.style.display === 'none';
    row.style.display = isHidden ? '' : 'none';
    
    // Update button styling and icon
    if (btn) {
      if (isHidden) {
        btn.classList.add('active');
        btn.innerHTML = '<i class="bi bi-chevron-up"></i>';
        // Auto-load tracklist when expanded if not already loaded.
        setTimeout(() => {
          const contentDiv = document.getElementById(contentId);
          if (contentDiv && contentDiv.innerHTML.trim() === '') {
            const trackedRow = document.getElementById(rowId);
            const loadBtn = trackedRow?.querySelector('button.btn-tracklist-load');
            if (loadBtn) {
              loadBtn.click();
            } else {
              // Discovered releases do not have a "Load" button; fetch directly.
              loadTracklist(artist, album, null, null);
            }
          }
        }, 100);
      } else {
        btn.classList.remove('active');
        btn.innerHTML = '<i class="bi bi-list-ul"></i>';
      }
    }
  }
}

function downloadTrack(searchQuery, artist, title) {
  if (!confirm(`Download "${title}" by ${artist}?`)) return;
  
  const btn = event.target.closest('button');
  const originalHTML = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
  
  // Initiate download via Soulseek
  fetch('/api/slskd/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query: searchQuery })
  })
  .then(response => response.json())
  .then(data => {
    if (data.error) {
      alert('❌ Error: ' + data.error);
      btn.disabled = false;
      btn.innerHTML = originalHTML;
      return;
    }
    
    const searchId = data.searchId;
    // Poll for results with a shorter timeout since we just want the first match
    pollSlskdResultsForDownload(searchId, artist, title, btn, originalHTML);
  })
  .catch(error => {
    alert('❌ Network error: ' + error.message);
    btn.disabled = false;
    btn.innerHTML = originalHTML;
  });
}

function pollSlskdResultsForDownload(searchId, artist, title, btn, originalHTML, pollCount = 0) {
  if (pollCount > 60) {
    // Timeout - try again or show manual search
    btn.disabled = false;
    btn.innerHTML = originalHTML;
    alert('⏱️ Search timed out. Try downloading manually from the Downloads page.');
    return;
  }
  
  fetch(`/api/slskd/search/${searchId}`)
    .then(response => response.json())
    .then(data => {
      if (!data.isComplete) {
        setTimeout(() => pollSlskdResultsForDownload(searchId, artist, title, btn, originalHTML, pollCount + 1), 1000);
        return;
      }
      
      if (data.fileCount === 0 || !data.results || data.results.length === 0) {
        btn.disabled = false;
        btn.innerHTML = originalHTML;
        alert('❌ No results found. Try searching manually.');
        return;
      }
      
      // Find best match (prefer complete albums/ep, then by size and seeders)
      const bestResult = data.results[0]; // Soulseek API should rank by relevance
      
      // Download the best match
      downloadSlskdFile(bestResult.username, bestResult.filename, bestResult.size || 0, btn, originalHTML);
    })
    .catch(error => {
      btn.disabled = false;
      btn.innerHTML = originalHTML;
      alert('❌ Search error: ' + error.message);
    });
}

function downloadSlskdFile(username, filename, size, btn, originalHTML) {
  fetch('/api/slskd/download-single', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      username: username,
      filename: filename,
      size: parseInt(size)
    })
  })
  .then(response => response.json())
  .then(data => {
    btn.disabled = false;
    if (data.error) {
      btn.innerHTML = originalHTML;
      alert('❌ Error: ' + data.error);
    } else {
      btn.innerHTML = '<i class="bi bi-check-circle"></i>';
      btn.classList.add('btn-success');
      btn.classList.remove('btn-outline-success');
      setTimeout(() => {
        btn.innerHTML = originalHTML;
        btn.classList.remove('btn-success');
        btn.classList.add('btn-outline-success');
      }, 2000);
      alert('✅ Download started!');
    }
  })
  .catch(error => {
    btn.disabled = false;
    btn.innerHTML = originalHTML;
    alert('❌ Network error: ' + error.message);
  });
}

function loadTracklist(artist, album, button = null, mbid = null) {
  const contentId = `tracklist-content-${artist.replace(/\s+/g, '_')}-${album.replace(/\s+/g, '_')}`;
  const contentDiv = document.getElementById(contentId);
  const spinner = button ? button.querySelector('.spinner-border') : null;
  
  if (!contentDiv) return;
  
  // Show spinner
  if (spinner) spinner.style.display = 'inline-block';
  if (button) button.disabled = true;
  
  // Build the tracklist fetch URL
  let tracklistUrl = `/api/album/tracklist?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(album)}`;
  if (mbid) {
    tracklistUrl += `&mbid=${encodeURIComponent(mbid)}`;
  }
  
  // Fetch and match tracklist
  Promise.all([
    fetch(tracklistUrl).then(r => r.json()),
    fetch(`/api/album/tracklist/match?artist=${encodeURIComponent(artist)}&album=${encodeURIComponent(album)}`).then(r => r.json())
  ])
  .then(([tracklistResponse, matchResponse]) => {
    if (spinner) spinner.style.display = 'none';
    if (button) button.disabled = false;
    
    if (tracklistResponse.error) {
      contentDiv.innerHTML = `<div class="alert alert-warning"><i class="bi bi-exclamation-triangle"></i> ${escapeHtml(tracklistResponse.error)}</div>`;
      return;
    }
    
    const tracklist = tracklistResponse.tracklist || [];
    const matched = new Set((matchResponse.matched || []).map(t => (t.title || '').toLowerCase()));
    const queued = new Set((matchResponse.queued || []).map(t => (t.title || '').toLowerCase()));
    
    if (tracklist.length === 0) {
      contentDiv.innerHTML = '<div class="alert alert-info">No tracks found</div>';
      return;
    }
    
    // Build tracklist HTML
    let html = `
      <div class="card border-info">
        <div class="card-header bg-info bg-opacity-10">
          <h6 class="mb-0"><i class="bi bi-list-ul"></i> Tracklist (${tracklist.length} tracks)</h6>
        </div>
        <div class="list-group list-group-flush">
    `;
    
    tracklist.forEach(track => {
      const normalizedTitle = (track.title || '').toLowerCase();
      const isQueued = queued.has(normalizedTitle);
      const isMatched = matched.has(normalizedTitle) && !isQueued;
      const matchBadge = isMatched
        ? '<span class="badge bg-success ms-2"><i class="bi bi-check-circle"></i> In Library</span>'
        : (isQueued
          ? '<span class="badge bg-warning text-dark ms-2"><i class="bi bi-hourglass-split"></i> In Queue</span>'
          : '');
      const rowClass = isQueued ? 'list-group-item-warning' : '';
      
      // Build search query for download (artist + title)
      const searchQuery = `${artist} ${track.title}`;
      
      // Add action buttons for tracks in library
      const actionButtons = isMatched ? `
        <div class="d-flex gap-2" style="flex-shrink: 0;">
          <button class="btn btn-sm btn-outline-primary" onclick="openEditTrackFromArtistModal('${escapeJsString(track.track_id || '')}', '${escapeJsString(track.title)}')" title="Edit track" style="flex-shrink: 0;">
            <i class="bi bi-pencil"></i>
          </button>
          <a href="/track/${escapeJsString(track.track_id || '')}" class="btn btn-sm btn-outline-info" title="View track page" style="flex-shrink: 0;">
            <i class="bi bi-arrow-right"></i>
          </a>
        </div>
      ` : '';
      
      html += `
        <div class="list-group-item ${rowClass}">
          <div class="d-flex justify-content-between align-items-center">
            <div class="flex-grow-1">
              <span class="text-muted small" style="min-width: 2em; text-align: right; display: inline-block;">
                ${String(track.position || '').padStart(2, '0')}
              </span>
              <span class="ms-2">${escapeHtml(track.title || '')}</span>
              ${matchBadge}
            </div>
            <div class="d-flex align-items-center gap-2">
              <small class="text-muted me-2" style="max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${escapeHtml(track.artist || '')}</small>
              ${actionButtons}
              <button class="btn btn-sm btn-outline-success" onclick="downloadTrack('${escapeJsString(searchQuery)}', '${escapeJsString(artist)}', '${escapeJsString(track.title)}')" title="Download this track" style="flex-shrink: 0;" ${isQueued ? 'disabled' : ''}>
                <i class="bi bi-download"></i>
              </button>
            </div>
          </div>
        </div>
      `;
    });
    
    html += `
        </div>
        <div class="card-footer text-muted small">
          <i class="bi bi-info-circle"></i> Green checkmarks = already in library. Yellow badges = already in queue. Click download button to add missing tracks from Soulseek.
        </div>
      </div>
    `;
    
    contentDiv.innerHTML = html;
  })
  .catch(error => {
    if (spinner) spinner.style.display = 'none';
    button.disabled = false;
    contentDiv.innerHTML = `<div class="alert alert-danger"><i class="bi bi-x-circle"></i> Error: ${escapeHtml(error.message)}</div>`;
  });
}

// Toggle missing items visibility
function toggleMissing(category) {
  const section = document.getElementById(category + '-section');
  if (!section) return;
  
  const tbody = section.querySelector('.' + category + '-tbody');
  const button = section.querySelector('.toggle-missing-btn');
  if (!tbody || !button) return;
  
  const isShowing = button.getAttribute('data-show') === 'true';
  const missingRows = tbody.querySelectorAll('tr[data-status="missing"]');
  
  if (isShowing) {
    // Hide missing
    missingRows.forEach(row => row.style.display = 'none');
    button.setAttribute('data-show', 'false');
    button.innerHTML = '<i class="bi bi-eye-slash"></i> <span class="d-none d-sm-inline">Show Missing</span>';
  } else {
    // Show missing
    missingRows.forEach(row => row.style.display = '');
    button.setAttribute('data-show', 'true');
    button.innerHTML = '<i class="bi bi-eye"></i> <span class="d-none d-sm-inline">Hide Missing</span>';
  }
  
  // Save preference to localStorage
  localStorage.setItem('showMissing-' + category, !isShowing);
}

// Initialize toggle states on page load
function initializeMissingToggle() {
  const categories = ['albums', 'compilations', 'live-albums', 'remix-albums', 'eps', 'singles'];
  categories.forEach(category => {
    const section = document.getElementById(category + '-section');
    if (!section) return;
    
    const tbody = section.querySelector('.' + category + '-tbody');
    const button = section.querySelector('.toggle-missing-btn');
    if (!tbody || !button) return;
    
    // Load preference from localStorage (default is false = hidden)
    const showMissing = localStorage.getItem('showMissing-' + category) === 'true';
    const missingRows = tbody.querySelectorAll('tr[data-status="missing"]');
    
    if (!showMissing) {
      // Hide by default
      missingRows.forEach(row => row.style.display = 'none');
      button.setAttribute('data-show', 'false');
      button.innerHTML = '<i class="bi bi-eye-slash"></i> <span class="d-none d-sm-inline">Show Missing</span>';
    } else {
      // Show if saved preference is true
      missingRows.forEach(row => row.style.display = '');
      button.setAttribute('data-show', 'true');
      button.innerHTML = '<i class="bi bi-eye"></i> <span class="d-none d-sm-inline">Hide Missing</span>';
    }
  });
}

// Initialize on page load
document.addEventListener('DOMContentLoaded', initializeMissingToggle);

// Search for all releases on MusicBrainz for an artist
function searchMusicBrainzForAllReleases(artistName) {
  // Show the MusicBrainz modal if it exists
  const modalElement = document.getElementById('musicBrainzModal');
  if (!modalElement) {
    alert('MusicBrainz search modal not found. Please ensure the modal is properly included.');
    return;
  }
  
  // Reset modal content
  const mbSearchInfo = document.getElementById('mbSearchInfo');
  const mbSearchStatus = document.getElementById('mbSearchStatus');
  const mbSearchError = document.getElementById('mbSearchError');
  const mbSearchResults = document.getElementById('mbSearchResults');
  const mbSearchArtist = document.getElementById('mbSearchArtist');
  
  if (mbSearchInfo) mbSearchInfo.style.display = 'none';
  if (mbSearchError) mbSearchError.style.display = 'none';
  if (mbSearchResults) mbSearchResults.innerHTML = '';
  if (mbSearchStatus) mbSearchStatus.style.display = 'none';
  
  // Show the modal
  const hasBootstrapModal = !!(window.bootstrap && window.bootstrap.Modal);
  if (hasBootstrapModal) {
    const modal = new bootstrap.Modal(modalElement);
    modal.show();
  } else {
    // Fallback for environments where Bootstrap JS is not loaded.
    modalElement.style.display = 'block';
    modalElement.classList.add('show');
    modalElement.removeAttribute('aria-hidden');
    modalElement.setAttribute('aria-modal', 'true');
    document.body.classList.add('modal-open');
  }
  
  // Trigger the search after modal is shown
  setTimeout(() => {
    performMusicBrainzSearchForArtist(artistName);
  }, 500);
}

function performMusicBrainzSearchForArtist(artistName) {
  const mbSearchInfo = document.getElementById('mbSearchInfo');
  const mbSearchStatus = document.getElementById('mbSearchStatus');
  const mbSearchError = document.getElementById('mbSearchError');
  const mbSearchResults = document.getElementById('mbSearchResults');
  const mbSearchArtist = document.getElementById('mbSearchArtist');
  
  // Show search info
  if (mbSearchArtist) mbSearchArtist.textContent = artistName;
  if (mbSearchInfo) mbSearchInfo.style.display = 'block';
  if (mbSearchStatus) mbSearchStatus.style.display = 'block';
  
  // Fetch and display artist releases from MusicBrainz
  fetch('/api/artist/missing-releases?artist=' + encodeURIComponent(artistName))
    .then(r => r.json())
    .then(data => {
      if (mbSearchStatus) mbSearchStatus.style.display = 'none';
      
      // Handle errors
      if (data.error) {
        throw new Error(data.error);
      }
      
      const missing = data.missing || [];
      
      if (missing.length === 0) {
        if (mbSearchResults) {
          mbSearchResults.innerHTML = `
            <div class="alert alert-info">
              <i class="bi bi-info-circle"></i> No releases found for this artist on MusicBrainz, or all releases are already in your library.
            </div>
          `;
        }
        return;
      }
      
      // Group by type
      const byType = {
        Album: [],
        Compilation: [],
        'Live Album': [],
        EP: [],
        Single: [],
        Other: []
      };
      
      missing.forEach(release => {
        const type = release.category || release.primary_type || 'Album';
        if (!byType[type]) byType[type] = [];
        byType[type].push(release);
      });
      
      // Build results HTML
      let html = `
        <div class="accordion" id="mbResultsAccordion">
      `;
      
      Object.entries(byType).forEach(([type, releases]) => {
        if (releases.length === 0) return;
        
        const itemId = 'accordion-' + type.replace(/\s+/g, '-').toLowerCase();
        
        html += `
          <div class="accordion-item">
            <h2 class="accordion-header">
              <button class="accordion-button${type !== 'Album' ? ' collapsed' : ''}" type="button" data-bs-toggle="collapse" data-bs-target="#${itemId}" aria-expanded="${type === 'Album'}" aria-controls="${itemId}">
                <i class="bi bi-disc"></i> ${type} (${releases.length})
              </button>
            </h2>
            <div id="${itemId}" class="accordion-collapse ${type !== 'Album' ? 'collapse' : 'collapse show'}" data-bs-parent="#mbResultsAccordion">
              <div class="accordion-body p-0">
                <div class="list-group list-group-flush">
        `;
        
        releases.forEach(release => {
          const year = release.first_release_date ? release.first_release_date.substring(0, 4) : 'Unknown';
          const coverUrl = release.cover_art_url || '';
          const coverImg = coverUrl ? `<img src="${escapeHtml(coverUrl)}" alt="${escapeHtml(release.title)}" style="width: 40px; height: 40px; object-fit: cover; border-radius: 3px; margin-right: 0.5rem;">` : '<i class="bi bi-disc" style="font-size: 1.5rem; margin-right: 0.5rem; opacity: 0.5;"></i>';
          
          html += `
            <div class="list-group-item d-flex align-items-center justify-content-between">
              <div class="d-flex align-items-center" style="flex: 1; min-width: 0;">
                ${coverImg}
                <div style="min-width: 0; flex: 1;">
                  <div class="text-truncate"><strong>${escapeHtml(release.title)}</strong></div>
                  <small class="text-muted">${escapeHtml(year)}</small>
                </div>
              </div>
              <button class="btn btn-sm btn-outline-success ms-2" onclick="addMissingReleaseForDownload('${escapeJsString(release.release_id)}', '${escapeJsString(release.title)}', '${escapeJsString(artistName)}')" title="Add to query for download search">
                <i class="bi bi-download"></i> Download
              </button>
            </div>
          `;
        });
        
        html += `
                </div>
              </div>
            </div>
          </div>
        `;
      });
      
      html += `</div>`;
      
      if (mbSearchResults) mbSearchResults.innerHTML = html;
    })
    .catch(error => {
      if (mbSearchStatus) mbSearchStatus.style.display = 'none';
      if (mbSearchError) {
        mbSearchError.textContent = error.message;
        mbSearchError.style.display = 'block';
      }
    });
}

function addMissingReleaseForDownload(releaseId, releaseTitle, artistName) {
  // Use the MusicBrainz search flow (same as upcoming releases) to find and download the album.
  // This will search MusicBrainz for the specific release and show all available versions
  // with track listings, allowing the user to pick the right version to download.
  searchMusicBrainzRelease(null, artistName, releaseTitle);
}

// ── Favourite Artist ────────────────────────────────────────────────────────

async function loadArtistFavouriteState(artistName) {
  try {
    const resp = await fetch('/api/artist/favourite?artist=' + encodeURIComponent(artistName));
    const data = await resp.json();
    updateFavouriteButtonState(data.is_favourite);
  } catch (e) {
    console.warn('Could not load favourite state:', e);
  }
}

function updateFavouriteButtonState(isFavourite) {
  const btn = document.getElementById('artistFavouriteBtn');
  const icon = document.getElementById('artistFavouriteIcon');
  const label = document.getElementById('artistFavouriteLabel');
  if (!btn) return;
  if (isFavourite) {
    btn.classList.remove('btn-outline-danger');
    btn.classList.add('btn-danger');
    if (icon) { icon.classList.remove('bi-heart'); icon.classList.add('bi-heart-fill'); }
    if (label) label.textContent = 'Favourited';
  } else {
    btn.classList.remove('btn-danger');
    btn.classList.add('btn-outline-danger');
    if (icon) { icon.classList.remove('bi-heart-fill'); icon.classList.add('bi-heart'); }
    if (label) label.textContent = 'Favourite';
  }
}

async function toggleArtistFavourite(artistName) {
  try {
    const btn = document.getElementById('artistFavouriteBtn');
    const isFavourite = btn && btn.classList.contains('btn-danger');

    if (isFavourite) {
      await fetch('/api/artist/favourite?artist=' + encodeURIComponent(artistName), { method: 'DELETE' });
      updateFavouriteButtonState(false);
    } else {
      await fetch('/api/artist/favourite', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ artist: artistName })
      });
      updateFavouriteButtonState(true);
    }
  } catch (e) {
    console.error('Error toggling favourite:', e);
    alert('Error updating favourite status: ' + e.message);
  }
}

// ── Covered By Section ───────────────────────────────────────────────────────

async function loadArtistCoveredBy(artistName) {
  const container = document.getElementById('artistCoveredByContainer');
  const countEl = document.getElementById('artistCoveredByCount');
  if (!container) return;

  try {
    const resp = await fetch('/api/artist/covered-by?artist=' + encodeURIComponent(artistName));
    const raw = await resp.text();
    let data = {};

    try {
      data = raw ? JSON.parse(raw) : {};
    } catch (_) {
      const contentType = (resp.headers.get('content-type') || '').toLowerCase();
      if (contentType.includes('text/html') || raw.trim().startsWith('<!DOCTYPE') || raw.trim().startsWith('<html')) {
        throw new Error(`Server returned HTML instead of JSON (HTTP ${resp.status})`);
      }
      throw new Error(`Server returned non-JSON response (HTTP ${resp.status})`);
    }

    if (!resp.ok) {
      throw new Error(data.error || `HTTP ${resp.status}: ${resp.statusText}`);
    }

    const covers = data.covers || [];

    if (covers.length === 0) {
      container.innerHTML = `
        <div class="p-3 text-muted text-center">
          <i class="bi bi-info-circle"></i> No covers of ${escapeHtml(artistName)}'s songs found in your library yet.
          <br><small>Cover songs are detected automatically during popularity scans when lyricist/writer metadata is available.</small>
        </div>`;
      if (countEl) countEl.textContent = 'No covers found in library';
      return;
    }

    if (countEl) countEl.textContent = `${covers.length} cover${covers.length !== 1 ? 's' : ''} found in library`;

    let html = '<div class="table-responsive"><table class="table table-hover mb-0"><thead><tr>';
    html += '<th>Covering Artist</th><th>Song Title</th><th>Album</th><th class="text-center">Year</th></tr></thead><tbody>';

    covers.forEach(cover => {
      const artistUrl = `/artist/${encodeURIComponent(cover.artist)}`;
      const albumUrl = cover.album ? `/album/${encodeURIComponent(cover.artist)}/${encodeURIComponent(cover.album)}` : null;
      html += `<tr>
        <td><a href="${escapeHtml(artistUrl)}" class="text-decoration-none">${escapeHtml(cover.artist)}</a></td>
        <td>${escapeHtml(cover.title)}</td>
        <td>${albumUrl ? `<a href="${escapeHtml(albumUrl)}" class="text-decoration-none text-muted">${escapeHtml(cover.album)}</a>` : (cover.album ? escapeHtml(cover.album) : '—')}</td>
        <td class="text-center">${cover.year || '—'}</td>
      </tr>`;
    });

    html += '</tbody></table></div>';
    container.innerHTML = html;

  } catch (e) {
    console.error('Error loading covered-by data:', e);
    container.innerHTML = `<div class="p-3 text-danger text-center"><i class="bi bi-exclamation-triangle"></i> Error loading covers: ${escapeHtml(e.message)}</div>`;
  }
}

// ── Artist Corrections Banner + Missing Tracks Check ──────────────────────
(function initCorrectionsAndMissingTracks() {
  const artistNameEl = document.querySelector('[data-artist-name]');
  const artistNameForCorr = artistNameEl ? artistNameEl.dataset.artistName : '';
  if (!artistNameForCorr) return;

  // Show corrections banner (always show link to corrections page)
  const banner = document.getElementById('artist-corrections-banner');
  if (banner) banner.style.display = 'block';

  // Async: check albums with MB MBIDs for missing tracks
  const mbRows = document.querySelectorAll('tr[data-mb-mbid]');
  if (!mbRows || mbRows.length === 0) return;

  const checks = Array.from(mbRows).map(row => {
    const albumName = row.dataset.albumName;
    const mbMbid = row.dataset.mbMbid;
    if (!albumName || !mbMbid) return Promise.resolve(0);

    return fetch(`/api/album/missing-tracks?artist=${encodeURIComponent(artistNameForCorr)}&album=${encodeURIComponent(albumName)}`)
      .then(r => r.ok ? r.json() : { missing_count: 0 })
      .then(data => {
        const count = data.missing_count || 0;
        if (count > 0) {
          // Show badge on the status cell of this album row
          const badge = row.querySelector('.album-missing-tracks-badge');
          if (badge) {
            badge.textContent = `${count} track${count !== 1 ? 's' : ''} missing`;
            badge.style.display = 'inline-block';
          }
          // Show the "Search missing tracks" button
          const searchBtn = row.querySelector('.album-search-missing-btn');
          if (searchBtn) searchBtn.style.display = 'inline-block';
        }
        return count;
      })
      .catch(() => 0);
  });

  Promise.all(checks).then(counts => {
    const totalMissingTracks = counts.reduce((a, b) => a + b, 0);
    if (totalMissingTracks > 0) {
      const badge = document.getElementById('artist-missing-tracks-badge');
      if (badge) {
        badge.textContent = `${totalMissingTracks} missing track${totalMissingTracks !== 1 ? 's' : ''} in library`;
        badge.style.display = 'inline-block';
      }
    }
  });
})();
</script>
<script src="{{ url_for('static', filename='js/genre-utils.js') }}"></script>
<style>
/* Dark theme improvements for artist page */

.missing-release-card {
  background-color: var(--primary-bg);
  border: 1px solid var(--border-color);
  border-radius: 0.5rem;
  padding: 0.75rem;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
}

.missing-release-art img {
  width: 80px;
  height: 80px;
  border-radius: 0.35rem;
  object-fit: cover;
  border: 1px solid var(--border-color);
  background-color: #2a2a2a;
}

/* Fix outline buttons visibility on dark theme */
.btn-outline-primary {
    color: #0d6efd !important;
    border-color: #0d6efd !important;
    background-color: rgba(13, 110, 253, 0.1) !important;
}

.btn-outline-primary:hover {
    color: #fff !important;
    background-color: #0d6efd !important;
    border-color: #0d6efd !important;
}

.btn-outline-info {
    color: #0dcaf0 !important;
    border-color: #0dcaf0 !important;
    background-color: rgba(13, 202, 240, 0.1) !important;
}

.btn-outline-info:hover {
    color: #000 !important;
    background-color: #0dcaf0 !important;
    border-color: #0dcaf0 !important;
}

.btn-outline-secondary {
    color: #6c757d !important;
    border-color: #6c757d !important;
    background-color: rgba(108, 117, 125, 0.1) !important;
}

.btn-outline-secondary:hover {
    color: #fff !important;
    background-color: #6c757d !important;
    border-color: #6c757d !important;
}

.card {
    background-color: var(--secondary-bg) !important;
    border-color: var(--border-color) !important;
}

.card-header {
    background-color: var(--primary-bg) !important;
    border-bottom-color: var(--border-color) !important;
    color: var(--text-primary) !important;
}

.card-body {
    background-color: var(--secondary-bg) !important;
    color: var(--text-primary) !important;
}

.table-responsive {
    background-color: var(--secondary-bg) !important;
}

.table {
    color: var(--text-primary) !important;
    background-color: transparent !important;
}

.table thead {
    background-color: var(--primary-bg) !important;
    color: var(--text-primary) !important;
}

.table thead th {
    background-color: var(--primary-bg) !important;
    color: var(--text-primary) !important;
    border-color: var(--border-color) !important;
}

.table tbody {
    background-color: transparent !important;
}

.table tbody tr {
    background-color: transparent !important;
    border-color: var(--border-color) !important;
}

.table tbody td {
    background-color: transparent !important;
    color: var(--text-primary) !important;
    border-color: var(--border-color) !important;
}

.table-hover tbody tr:hover {
    background-color: var(--primary-bg) !important;
    color: var(--text-primary) !important;
}

.table-hover tbody tr:hover td {
    background-color: var(--primary-bg) !important;
}

.modal-content {
    background-color: var(--secondary-bg) !important;
    color: var(--text-primary) !important;
    border-color: var(--border-color) !important;
}

.modal-header {
    background-color: var(--primary-bg) !important;
    border-bottom-color: var(--border-color) !important;
    color: var(--text-primary) !important;
}

.modal-body {
    background-color: var(--secondary-bg) !important;
    color: var(--text-primary) !important;
}

.modal-body .form-control {
    background-color: var(--primary-bg) !important;
    border-color: var(--border-color) !important;
    color: var(--text-primary) !important;
}

.modal-body .form-control:focus {
    background-color: var(--primary-bg) !important;
    border-color: var(--spotify-green) !important;
    color: var(--text-primary) !important;
}

.modal-body .form-text {
    color: var(--text-secondary) !important;
}

.modal-body .table {
    background-color: transparent !important;
}

.modal-body .table thead {
    background-color: var(--primary-bg) !important;
}

.modal-body .table tbody {
    background-color: transparent !important;
}

.modal-body .table tbody tr {
    background-color: transparent !important;
}

.modal-body .table tbody td {
    background-color: transparent !important;
    color: var(--text-primary) !important;
}

.modal-body .table-hover tbody tr:hover {
    background-color: var(--primary-bg) !important;
}

.modal-body .table-hover tbody tr:hover td {
    background-color: var(--primary-bg) !important;
}

/* Similar artists and genres typography improvements */
#artist-similar-section .card-title,
#artist-genres-section .card-title {
  font-size: 1.2rem;
}

#artistSimilarArtistsContainer strong {
  font-size: 1.05rem;
}

#artistSimilarArtistsContainer .card-title {
  font-size: 1.02rem;
}

.similar-artist-pill {
  font-size: 0.95rem;
  padding: 0.45rem 0.6rem;
}

#artistGenreSourcesContainer,
#artistGenresContainer {
  font-size: 1rem;
}

.artist-genre-source-badge,
.artist-genre-badge,
#artistGenresContainer .badge {
  font-size: 0.95rem !important;
  padding: 0.45rem 0.65rem;
}

@media (max-width: 767.98px) {
  .artist-page .table-responsive {
    overflow-x: hidden;
  }

  .artist-page tr.album-row {
    display: block;
    margin-bottom: 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 0.5rem;
    background-color: var(--secondary-bg);
    overflow: hidden;
  }

  .artist-page tr.album-row td {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    width: 100%;
    padding: 0.5rem 0.75rem;
    border: 0 !important;
  }

  .artist-page tr.album-row td:nth-child(1) {
    justify-content: center;
    padding-top: 0.75rem;
  }

  .artist-page tr.album-row td:nth-child(1) img {
    width: 92px !important;
    height: 92px !important;
  }

  .artist-page tr.album-row td:nth-child(2) {
    justify-content: flex-start;
    font-weight: 600;
    padding-top: 0.25rem;
  }

  .artist-page tr.album-row td:nth-child(3)::before {
    content: 'Year';
    color: var(--text-secondary);
    font-size: 0.82rem;
  }

  .artist-page tr.album-row td:nth-child(4)::before {
    content: 'Status';
    color: var(--text-secondary);
    font-size: 0.82rem;
  }

  .artist-page tr.album-row td:nth-child(5)::before {
    content: 'Tracks';
    color: var(--text-secondary);
    font-size: 0.82rem;
  }

  .artist-page tr.album-row td:nth-child(6)::before {
    content: 'Avg';
    color: var(--text-secondary);
    font-size: 0.82rem;
  }

  .artist-page tr.album-row td:nth-child(7)::before {
    content: 'Actions';
    color: var(--text-secondary);
    font-size: 0.82rem;
  }

  .artist-page tr.album-row td:nth-child(7) .btn-group {
    width: 100%;
    justify-content: flex-end;
    flex-wrap: wrap;
  }

  .artist-page tr.tracklist-row td {
    padding: 0.5rem 0.25rem !important;
  }
}
</style>

<!-- Track Editing Modal -->
<div class="modal fade" id="editTrackModal" tabindex="-1">
  <div class="modal-dialog modal-dialog-centered">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title"><i class="bi bi-pencil-square"></i> Edit Track</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <input type="hidden" id="editTrackId">
        <input type="hidden" id="editTrackCurrentField">
        <div class="mb-3">
          <label class="form-label" id="editTrackLabel">Track Title</label>
          <input type="text" class="form-control" id="editTrackValue" placeholder="Enter value">
        </div>
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
        <button type="button" class="btn btn-primary" onclick="saveEditedTrackFromArtistPage()">Save Changes</button>
      </div>
    </div>
  </div>
</div>

<script>
  // Track editing for artist page
  function editTrackTitle(trackId, currentTitle) {
    const modal = new bootstrap.Modal(document.getElementById('editTrackModal'));
    document.getElementById('editTrackId').value = trackId;
    document.getElementById('editTrackCurrentField').value = 'title';
    document.getElementById('editTrackLabel').textContent = 'Track Title';
    document.getElementById('editTrackValue').value = currentTitle || '';
    document.getElementById('editTrackValue').focus();
    modal.show();
  }

  function saveEditedTrackFromArtistPage() {
    const trackId = document.getElementById('editTrackId').value;
    const field = document.getElementById('editTrackCurrentField').value;
    const value = document.getElementById('editTrackValue').value.trim();

    if (!trackId || !field) return;

    const payload = {
      track_id: trackId,
      [field]: value,
      sync_to_file: true
    };

    fetch('/api/track/update-metadata', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    })
      .then(r => r.json())
      .then(data => {
        if (data.success) {
          if (data.file_synced === false) {
            alert('⚠️ Track metadata saved to database, but file tags were not updated. Check file permissions/path and logs.');
          } else {
            alert('✅ Track metadata updated (database + file tags)');
          }
          bootstrap.Modal.getInstance(document.getElementById('editTrackModal')).hide();
          // Reload to show changes
          setTimeout(() => location.reload(), 1000);
        } else {
          alert('❌ Error: ' + (data.error || 'Failed to update'));
        }
      })
      .catch(err => {
        alert('❌ Network error: ' + err.message);
      });
  }

  document.addEventListener('DOMContentLoaded', function() {
    const artistName = {{ artist_name|tojson }};
    if (artistName && artistName.length > 0) {
      loadSimilarArtists(artistName);
      loadArtistFavouriteState(artistName);
      loadArtistCoveredBy(artistName);
      // Auto-refresh missing releases in the background so the sections are
      // always up-to-date without requiring a manual button click.
      checkMissingReleases(artistName, true, true);
    }
  });

  // Similar Artists Loading (fetch from API like album page does)
  async function loadSimilarArtists(artist) {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 15000); // 15 second timeout
    
    try {
      const url = `/api/artist/${encodeURIComponent(artist)}/similar`;
      console.log('[SIMILAR ARTISTS] Fetching from:', url);
      const response = await fetch(url, {
        signal: controller.signal
      });
      clearTimeout(timeoutId);
      
      console.log('[SIMILAR ARTISTS] Response status:', response.status);
      
      if (!response.ok) {
        const errorText = `Server returned ${response.status}${response.statusText ? ': ' + response.statusText : ''}`;
        console.error('[SIMILAR ARTISTS] Error response:', errorText);
        showArtistSimilarArtistsError(`Failed to load similar artists (${errorText})`);
        return;
      }
      
      const data = await response.json();
      console.log('[SIMILAR ARTISTS] Received data:', data);
      displayArtistSimilarArtists(data);
    } catch (error) {
      clearTimeout(timeoutId);
      console.error('[SIMILAR ARTISTS] Exception:', error.name, error.message);
      if (error.name === 'AbortError') {
        console.warn('Similar artists loading timeout (15s)');
        showArtistSimilarArtistsError('Similar artists loading timed out after 15 seconds');
      } else {
        console.error('Error loading similar artists:', error);
        showArtistSimilarArtistsError('Error loading similar artists: ' + error.message);
      }
    }
  }


  function displayArtistSimilarArtists(data) {
    const container = document.getElementById('artistSimilarArtistsContainer');
    
    // Validate data structure
    if (!data || !data.similar_artists) {
      container.innerHTML = '<div class="alert alert-info mb-0"><i class="bi bi-info-circle"></i> No similar artists data available</div>';
      return;
    }
    
    const lastfmArtists = data.similar_artists.lastfm || [];
    const listenbrainzArtists = data.similar_artists.listenbrainz || [];

    const normalizeSimilarArtist = (entry, source) => {
      if (typeof entry === 'string') {
        const name = entry.trim();
        return name ? { name, match: 0, source, in_collection: false } : null;
      }
      if (!entry || typeof entry !== 'object') return null;

      const name = String(entry.name || entry.artist || '').trim();
      if (!name) return null;

      const matchValue = Number(entry.match ?? entry.score ?? 0);
      return {
        ...entry,
        name,
        match: Number.isFinite(matchValue) ? matchValue : 0,
        source,
        in_collection: !!entry.in_collection
      };
    };

    // Combine and deduplicate similar artists
    const allArtists = [
      ...lastfmArtists.map(a => normalizeSimilarArtist(a, 'lastfm')).filter(Boolean),
      ...listenbrainzArtists.map(a => normalizeSimilarArtist(a, 'listenbrainz')).filter(Boolean)
    ];

    // Deduplicate by name (keep first occurrence, prefer in_collection=true)
    const seen = new Map();
    allArtists.forEach(a => {
      const key = a.name.toLowerCase();
      if (!seen.has(key) || a.in_collection) seen.set(key, a);
    });
    const uniqueArtists = Array.from(seen.values());
    
    if (uniqueArtists.length === 0) {
      container.innerHTML = '<div class="alert alert-info mb-0"><i class="bi bi-info-circle"></i> No similar artists available yet.<br><small>Similar artists are collected during popularity scans from Last.fm and ListenBrainz.</small></div>';
      return;
    }

    const inCollection = uniqueArtists.filter(a => a.in_collection);
    const notInCollection = uniqueArtists.filter(a => !a.in_collection);

    let html = '';

    // Section: Similar Artists (In Collection)
    if (inCollection.length > 0) {
      html += `
        <div class="mb-4">
          <h6 class="mb-2"><i class="bi bi-collection-fill text-success"></i> Similar Artists (In Collection) <span class="badge bg-success">${inCollection.length}</span></h6>
          <div class="d-flex flex-wrap gap-2">
            ${inCollection.map(a => `
              <a href="/artist/${encodeURIComponent(a.name)}" class="badge bg-success text-decoration-none similar-artist-pill" style="font-size: 0.85rem; padding: 0.4rem 0.7rem;">
                ${escapeHtml(a.name)}
              </a>
            `).join('')}
          </div>
        </div>
      `;
    }

    // Section: Similar Artists (Not in Collection)
    if (notInCollection.length > 0) {
      html += `
        <div>
          <h6 class="mb-3"><i class="bi bi-person-plus text-secondary"></i> Similar Artists (Not in Collection) <span class="badge bg-secondary">${notInCollection.length}</span></h6>
          <div class="row g-3">
      `;
      notInCollection.forEach(artist => {
        const matchPercent = artist.match ? Math.round(artist.match * 100) : 0;
        const matchBadge = matchPercent > 0 ? `<span class="badge bg-success position-absolute" style="top: 8px; right: 8px;">${matchPercent}%</span>` : '';
        const artistImageUrl = `/api/artist/image?name=${encodeURIComponent(artist.name)}`;
        html += `
          <div class="col-6 col-md-4 col-lg-3">
            <div class="card h-100 overflow-hidden" style="position: relative;">
              ${matchBadge}
              <div style="aspect-ratio: 1; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); display: flex; align-items: center; justify-content: center; position: relative;">
                <img src="${artistImageUrl}" alt="${escapeHtml(artist.name)}" style="width: 100%; height: 100%; object-fit: cover;" onerror="this.style.display='none'; this.nextElementSibling.style.display='block';">
                <i class="bi bi-person-circle" style="font-size: 3rem; color: white; opacity: 0.8; display: none;"></i>
              </div>
              <div class="card-body d-flex flex-column">
                <h6 class="card-title mb-2 fw-bold text-truncate" title="${escapeHtml(artist.name)}">${escapeHtml(artist.name)}</h6>
                <div class="btn-group-vertical btn-group-sm mt-auto" role="group">
                  <button class="btn btn-outline-secondary btn-sm" onclick="searchMusicBrainzReleaseFromEncoded(null, '${encodeURIComponent(artist.name)}', '')" title="Search MusicBrainz for releases">
                    <i class="bi bi-download"></i> Find Releases
                  </button>
                  <a href="https://www.last.fm/music/${encodeURIComponent(artist.name)}" target="_blank" class="btn btn-outline-info btn-sm" title="View on Last.fm">
                    <i class="bi bi-box-arrow-up-right"></i> Last.fm
                  </a>
                </div>
              </div>
            </div>
          </div>
        `;
      });
      html += `</div></div>`;
    }

    if (!html) {
      html = '<div class="alert alert-info mb-0"><i class="bi bi-info-circle"></i> No similar artists available</div>';
    }

    container.innerHTML = html;
  }

  function showArtistSimilarArtistsError(message) {
    const container = document.getElementById('artistSimilarArtistsContainer');
    container.innerHTML = `<div class="alert alert-danger mb-0"><i class="bi bi-exclamation-circle"></i> ${escapeHtml(message)}</div>`;
  }

  // ===== Track Edit Modal Functions for Artist Page =====
  let editArtistTrackCurrentGenres = [];
  let editArtistTrackModalInstance = null;

  const ARTIST_MODAL_ADVANCED_FIELDS = [
    { name: 'writer', label: 'Writer/Lyricist' },
    { name: 'arranger', label: 'Arranger' },
    { name: 'mixer', label: 'Mixer' },
    { name: 'producer', label: 'Producer' },
    { name: 'work', label: 'Work/Composition' },
    { name: 'isrc', label: 'ISRC' },
    { name: 'bpm', label: 'BPM' },
    { name: 'bitrate', label: 'Bitrate (kbps)' },
    { name: 'sample_rate', label: 'Sample Rate (Hz)' },
    { name: 'titlesort', label: 'Title Sort' },
    { name: 'albumsort', label: 'Album Sort' },
    { name: 'artistsort', label: 'Artist Sort' },
    { name: 'composersort', label: 'Composer Sort' },
    { name: 'albumartistsort', label: 'Album Artist Sort' },
    { name: 'lyricistsort', label: 'Lyricist Sort' },
    { name: 'artistssort', label: 'Artists Sort' },
    { name: 'albumartistssort', label: 'Album Artists Sort' },
    { name: 'artists', label: 'Artists (multi)' },
    { name: 'albumartists', label: 'Album Artists (multi)' },
    { name: 'conductor', label: 'Conductor' },
    { name: 'performer', label: 'Performer' },
    { name: 'director', label: 'Director' },
    { name: 'djmixer', label: 'DJ Mixer' },
    { name: 'engineer', label: 'Engineer' },
    { name: 'remixer', label: 'Remixer' },
    { name: 'lyricist', label: 'Lyricist' },
    { name: 'albumversion', label: 'Album Version' },
    { name: 'recordlabel', label: 'Record Label' },
    { name: 'copyright', label: 'Copyright' },
    { name: 'releasedate', label: 'Release Date' },
    { name: 'releasetype', label: 'Release Type' },
    { name: 'releasestatus', label: 'Release Status' },
    { name: 'releasecountry', label: 'Release Country' },
    { name: 'media', label: 'Media Format' },
    { name: 'barcode', label: 'Barcode' },
    { name: 'catalognumber', label: 'Catalog Number' },
    { name: 'asin', label: 'ASIN' },
    { name: 'originalyear', label: 'Original Year' },
    { name: 'originaldate', label: 'Original Date' },
    { name: 'tracktotal', label: 'Track Total' },
    { name: 'disctotal', label: 'Disc Total' },
    { name: 'script', label: 'Script' },
    { name: 'discsubtitle', label: 'Disc Subtitle' },
    { name: 'subtitle', label: 'Subtitle' },
    { name: 'grouping', label: 'Grouping' },
    { name: 'movement', label: 'Movement' },
    { name: 'movementname', label: 'Movement Name' },
    { name: 'movementtotal', label: 'Movement Total' },
    { name: 'key', label: 'Musical Key' },
    { name: 'language', label: 'Language' },
    { name: 'license', label: 'License' },
    { name: 'website', label: 'Website' },
    { name: 'encodedby', label: 'Encoded By' },
    { name: 'encodersettings', label: 'Encoder Settings' },
    { name: 'explicitstatus', label: 'Explicit Status' },
    { name: 'musicbrainz_albumid', label: 'MusicBrainz Album ID' },
    { name: 'musicbrainz_artistid', label: 'MusicBrainz Artist ID' },
    { name: 'musicbrainz_albumartistid', label: 'MusicBrainz Album Artist ID' },
    { name: 'musicbrainz_releasegroupid', label: 'MusicBrainz Release Group ID' },
    { name: 'musicbrainz_releasetrackid', label: 'MusicBrainz Release Track ID' },
    { name: 'musicbrainz_workid', label: 'MusicBrainz Work ID' },
    { name: 'replaygain_track_gain', label: 'ReplayGain Track Gain' },
    { name: 'replaygain_track_peak', label: 'ReplayGain Track Peak' },
    { name: 'replaygain_album_gain', label: 'ReplayGain Album Gain' },
    { name: 'replaygain_album_peak', label: 'ReplayGain Album Peak' },
    { name: 'r128_track_gain', label: 'R128 Track Gain' },
    { name: 'r128_album_gain', label: 'R128 Album Gain' },
    { name: 'lyrics', label: 'Lyrics', type: 'textarea' }
  ];

  function renderArtistAdvancedTrackFields(trackData) {
    const container = document.getElementById('editArtistTrackAdvancedFields');
    if (!container) return;

    container.innerHTML = ARTIST_MODAL_ADVANCED_FIELDS.map(def => {
      const fieldId = `editArtistTrackAdv_${def.name}`;
      if (def.type === 'textarea') {
        return `
          <div class="col-12">
            <label for="${fieldId}" class="form-label">${def.label}</label>
            <textarea class="form-control" id="${fieldId}" rows="4"></textarea>
          </div>
        `;
      }
      return `
        <div class="col-md-6">
          <label for="${fieldId}" class="form-label">${def.label}</label>
          <input type="text" class="form-control" id="${fieldId}">
        </div>
      `;
    }).join('');

    ARTIST_MODAL_ADVANCED_FIELDS.forEach(def => {
      const el = document.getElementById(`editArtistTrackAdv_${def.name}`);
      if (el) {
        el.value = trackData?.[def.name] || '';
      }
    });
  }

  function openEditTrackFromArtistModal(trackId, trackTitle) {
    fetch(`/api/track/${trackId}`)
      .then(r => {
        if (!r.ok) throw new Error('Track not found');
        return r.json();
      })
      .then(trackData => {
        openComprehensiveEditArtistTrackModal(trackId, trackData);
      })
      .catch(err => {
        alert('❌ Error loading track: ' + err.message);
      });
  }

  function openComprehensiveEditArtistTrackModal(trackId, trackData) {
    // Set track ID
    document.getElementById('editArtistTrackId').value = trackId;
    
    // Populate form with track data
    document.getElementById('editArtistTrackTitle').textContent = trackData.title || 'Unknown';
    document.getElementById('editArtistTrackTitleField').value = trackData.title || '';
    document.getElementById('editArtistTrackArtistField').value = trackData.artist || '';
    document.getElementById('editArtistTrackAlbumField').value = trackData.album || '';
    document.getElementById('editArtistTrackYearField').value = trackData.year || '';
    document.getElementById('editArtistTrackStarsField').value = trackData.stars || 0;
    document.getElementById('editArtistTrackSingleField').value = trackData.is_single || 0;
    document.getElementById('editArtistTrackConfidenceField').value = trackData.single_confidence || 'low';
    document.getElementById('editArtistTrackAlbumArtistField').value = trackData.album_artist || '';
    document.getElementById('editArtistTrackComposerField').value = trackData.composer || '';
    document.getElementById('editArtistTrackTrackNumberField').value = trackData.track_number || '';
    document.getElementById('editArtistTrackDiscNumberField').value = trackData.disc_number || '';
    document.getElementById('editArtistTrackMBIDField').value = trackData.mbid || '';
    document.getElementById('editArtistTrackCommentField').value = trackData.comment || '';
    renderArtistAdvancedTrackFields(trackData || {});
    
    // Handle genres
    editArtistTrackCurrentGenres = [];
    if (trackData.genres) {
      editArtistTrackCurrentGenres = trackData.genres.split(/[;,\\\/]/).map(g => g.trim()).filter(g => g);
    }
    updateEditArtistTrackGenresDisplay();
    
    // Load and display recommended genres
    loadRecommendedGenresForTrack(trackData.artist, trackId);
    
    // Show modal
    if (!editArtistTrackModalInstance) {
      editArtistTrackModalInstance = new bootstrap.Modal(document.getElementById('editTrackFromArtistModal'));
    }
    editArtistTrackModalInstance.show();
  }

  function loadRecommendedGenresForTrack(artist, trackId) {
    const section = document.getElementById('artistRecommendedGenresSection');
    const display = document.getElementById('artistRecommendedGenresDisplay');
    
    if (!section || !display) return;
    
    // Fetch genres from all sources for this track
    fetch(`/api/genres/track/${trackId}`)
      .then(r => r.json())
      .then(data => {
        if (!data.genres) return;
        
        // Collect all unique genres from Last.fm and Discogs sources
        const recommendedGenres = new Map();
        
        // Collect from Last.fm tags
        if (data.genres.lastfm_tags) {
          data.genres.lastfm_tags.forEach(genre => {
            const name = typeof genre === 'object' ? genre.name : genre;
            recommendedGenres.set(name, (recommendedGenres.get(name) || 0) + 1);
          });
        }
        
        // Collect from Discogs genres
        if (data.genres.discogs_genres) {
          data.genres.discogs_genres.forEach(genre => {
            const name = typeof genre === 'object' ? genre.name : genre;
            recommendedGenres.set(name, (recommendedGenres.get(name) || 0) + 1);
          });
        }
        
        // Collect from Spotify
        if (data.genres.spotify_genres) {
          data.genres.spotify_genres.forEach(genre => {
            const name = typeof genre === 'object' ? genre.name : genre;
            recommendedGenres.set(name, (recommendedGenres.get(name) || 0) + 1);
          });
        }
        
        if (recommendedGenres.size > 0) {
          section.style.display = 'block';
          
          let genresHtml = '';
          recommendedGenres.forEach((count, genre) => {
            genresHtml += `<button type="button" class="btn btn-sm btn-outline-info" onclick="addEditArtistGenreFromRecommended('${escapeJsString(genre)}')" title="Add to track genres">
              ${escapeHtml(genre)}
              <small class="text-muted ms-1">(${count})</small>
            </button>`;
          });
          
          display.innerHTML = genresHtml;
        } else {
          section.style.display = 'none';
        }
      })
      .catch(err => {
        section.style.display = 'none';
        console.warn('Could not load recommended genres:', err);
      });
  }

  function addEditArtistGenreFromRecommended(genre) {
    if (!editArtistTrackCurrentGenres.includes(genre)) {
      editArtistTrackCurrentGenres.push(genre);
      updateEditArtistTrackGenresDisplay();
    }
  }

  function updateEditArtistTrackGenresDisplay() {
    const container = document.getElementById('editArtistTrackGenresDisplay');
    container.innerHTML = '';
    
    if (editArtistTrackCurrentGenres.length === 0) {
      container.innerHTML = '<span class="text-muted small">No genres set</span>';
    } else {
      editArtistTrackCurrentGenres.forEach(genre => {
        const badge = document.createElement('span');
        badge.className = 'badge bg-primary me-1 mb-1';
        badge.style.fontSize = '0.9rem';
        badge.innerHTML = `${genre} <button type="button" class="btn-close btn-close-white ms-1" style="font-size: 0.6rem;" onclick="removeEditArtistTrackGenre('${escapeJsString(genre)}')" aria-label="Remove"></button>`;
        container.appendChild(badge);
      });
    }
    document.getElementById('editArtistTrackGenresField').value = editArtistTrackCurrentGenres.join('\\');
  }

  function addEditArtistTrackGenre() {
    const input = document.getElementById('editArtistTrackGenreInput');
    const genre = input.value.trim();
    
    if (!genre) return;
    
    if (!editArtistTrackCurrentGenres.includes(genre)) {
      editArtistTrackCurrentGenres.push(genre);
      updateEditArtistTrackGenresDisplay();
    }
    
    input.value = '';
    input.focus();
  }

  function removeEditArtistTrackGenre(genre) {
    editArtistTrackCurrentGenres = editArtistTrackCurrentGenres.filter(g => g !== genre);
    updateEditArtistTrackGenresDisplay();
  }

  function saveArtistEditedTrack() {
    const trackId = document.getElementById('editArtistTrackId').value;
    
    if (!trackId) {
      alert('❌ Error: No track ID');
      return;
    }
    
    // Build payload from form
    const payload = {
      track_id: trackId,
      title: document.getElementById('editArtistTrackTitleField').value.trim(),
      artist: document.getElementById('editArtistTrackArtistField').value.trim(),
      album: document.getElementById('editArtistTrackAlbumField').value.trim(),
      year: document.getElementById('editArtistTrackYearField').value.trim() || null,
      stars: parseInt(document.getElementById('editArtistTrackStarsField').value) || 0,
      is_single: parseInt(document.getElementById('editArtistTrackSingleField').value) || 0,
      single_confidence: document.getElementById('editArtistTrackConfidenceField').value,
      genres: editArtistTrackCurrentGenres.join('\\'),
      album_artist: document.getElementById('editArtistTrackAlbumArtistField').value.trim() || null,
      composer: document.getElementById('editArtistTrackComposerField').value.trim() || null,
      track_number: document.getElementById('editArtistTrackTrackNumberField').value.trim() || null,
      disc_number: document.getElementById('editArtistTrackDiscNumberField').value.trim() || null,
      mbid: document.getElementById('editArtistTrackMBIDField').value.trim() || null,
      comment: document.getElementById('editArtistTrackCommentField').value.trim() || null,
      sync_to_file: true
    };

    ARTIST_MODAL_ADVANCED_FIELDS.forEach(def => {
      const el = document.getElementById(`editArtistTrackAdv_${def.name}`);
      if (!el) return;
      const raw = (el.value || '').trim();
      payload[def.name] = raw || null;
    });
    
    // Validate required fields
    if (!payload.title) {
      alert('❌ Error: Title is required');
      return;
    }
    
    // Send to API
    fetch('/api/track/update-metadata', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    })
    .then(r => r.json())
    .then(data => {
      if (data.success) {
        if (editArtistTrackModalInstance) {
          editArtistTrackModalInstance.hide();
        }
        if (data.file_synced === false) {
          alert('⚠️ Track metadata saved to database, but file tags were not updated. Check file permissions/path and logs.');
        } else {
          alert('✅ Track metadata updated successfully (database + file tags)');
        }
        
        // Reload to show updated data
        setTimeout(() => {
          location.reload();
        }, 500);
      } else {
        alert('❌ Error: ' + (data.error || 'Failed to update'));
      }
    })
    .catch(err => {
      alert('❌ Network error: ' + err.message);
    });
  }

  // ===== Smart Artist Routing (shared with track.html) =====
  /**
   * Navigate to an artist page or open MusicBrainz lookup if artist doesn't exist.
   * If artist exists in database, navigates to their page.
   * If not, opens a MusicBrainz/Discogs lookup modal with options to add releases.
   */
  async function navigateToArtist(artistName, event) {
    event?.preventDefault();
    
    if (!artistName) return;
    
    try {
      const response = await fetch(`/api/artist/exists?artist=${encodeURIComponent(artistName)}`);
      const data = await response.json();
      
      if (data.exists) {
        // Artist exists - navigate to their page
        window.location.href = `/artist/${encodeURIComponent(artistName)}`;
      } else {
        // Artist doesn't exist - open MusicBrainz lookup
        openArtistMusicBrainzLookup(artistName);
      }
    } catch (error) {
      console.error('Error checking artist existence:', error);
      // On error, try to navigate anyway
      window.location.href = `/artist/${encodeURIComponent(artistName)}`;
    }
  }

  /**
   * Open MusicBrainz / Discogs lookup modal for an artist.
   * Searches both sources, displays albums, and allows adding to download queue.
   */
  function openArtistMusicBrainzLookup(artistName) {
    // Create lookup modal if it doesn't exist
    let modal = document.getElementById('artistMBLookupModal');
    if (!modal) {
      const modalHTML = `
        <div class="modal fade" id="artistMBLookupModal" tabindex="-1" aria-labelledby="artistMBLookupLabel" aria-hidden="true">
          <div class="modal-dialog modal-xl modal-dialog-scrollable">
            <div class="modal-content">
              <div class="modal-header">
                <h5 class="modal-title" id="artistMBLookupLabel">
                  <i class="bi bi-person-heart"></i> Add Artist to Library
                </h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
              </div>
              <div class="modal-body">
                <div id="artistMBLookupInfo" class="alert alert-info mb-3" style="display:none;">
                  Searching for <strong id="artistMBLookupName"></strong>...
                </div>
                
                <div id="artistMBLookupLoading" class="text-center py-5">
                  <div class="spinner-border text-primary" role="status">
                    <span class="visually-hidden">Loading...</span>
                  </div>
                  <p class="mt-3">Searching MusicBrainz and Discogs...</p>
                </div>
                
                <div id="artistMBLookupError" class="alert alert-danger" style="display:none;"></div>
                
                <div id="artistMBLookupResults"></div>
              </div>
            </div>
          </div>
        </div>
      `;
      document.body.insertAdjacentHTML('beforeend', modalHTML);
      modal = document.getElementById('artistMBLookupModal');
    }
    
    const bsModal = new bootstrap.Modal(modal);
    bsModal.show();
    
    // Perform search
    performArtistMusicBrainzSearch(artistName);
  }

  /**
   * Search MusicBrainz and Discogs for artist and display results.
   */
  async function performArtistMusicBrainzSearch(artistName) {
    const loadingDiv = document.getElementById('artistMBLookupLoading');
    const errorDiv = document.getElementById('artistMBLookupError');
    const resultsDiv = document.getElementById('artistMBLookupResults');
    const infoDiv = document.getElementById('artistMBLookupInfo');
    const nameSpan = document.getElementById('artistMBLookupName');
    
    loadingDiv.style.display = 'block';
    errorDiv.style.display = 'none';
    resultsDiv.innerHTML = '';
    infoDiv.style.display = 'block';
    nameSpan.textContent = artistName;
    
    try {
      // Search MusicBrainz for artist releases
      const mbResponse = await fetch('/api/musicbrainz/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: artistName, artist_only: true })
      });
      
      const mbData = await mbResponse.json();
      loadingDiv.style.display = 'none';
      
      if (mbData.error) {
        errorDiv.textContent = 'Search error: ' + mbData.error;
        errorDiv.style.display = 'block';
        return;
      }
      
      const releases = mbData.releases || [];
      
      if (releases.length === 0) {
        resultsDiv.innerHTML = `
          <div class="alert alert-info">
            <i class="bi bi-info-circle"></i> No releases found for "${escapeHtml(artistName)}".
          </div>
        `;
        return;
      }
      
      // Display releases
      let html = '<div class="row g-3">';
      releases.forEach(release => {
        const coverArt = release.cover_art_url || 'data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%22150%22 height=%22150%22%3E%3Crect fill=%22%23333%22 width=%22150%22 height=%22150%22/%3E%3Ctext x=%2250%25%22 y=%2250%25%22 text-anchor=%22middle%22 dy=%22.3em%22 fill=%22%23666%22 text-anchor=%22middle%22%3ENo Art%3C/text%3E%3C/svg%3E';
        const releaseDate = release.first_release_date || 'Unknown';
        const releaseType = release.category || release.primary_type || 'Release';
        const artist = release.artist || 'Unknown Artist';
        
        html += `
          <div class="col-12 col-sm-6 col-md-4">
            <div class="card h-100">
              <img src="${coverArt}" class="card-img-top" alt="${escapeHtml(release.title)}" style="height: 150px; object-fit: cover;" onerror="this.src='data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%22150%22 height=%22150%22%3E%3Crect fill=%22%23333%22 width=%22150%22 height=%22150%22/%3E%3C/svg%3E'">
              <div class="card-body d-flex flex-column">
                <h6 class="card-title text-truncate">${escapeHtml(release.title)}</h6>
                <p class="card-text small text-muted mb-2">
                  <span class="badge bg-secondary">${releaseType}</span>
                  <span class="badge bg-info ms-1">${releaseDate}</span>
                </p>
                <div class="mt-auto">
                  <button class="btn btn-sm btn-primary w-100" onclick="openSlskdSearch('${escapeJsString(release.title)}', '${escapeJsString(artist)}'); bootstrap.Modal.getInstance(document.getElementById('artistMBLookupModal'))?.hide();">
                    <i class="bi bi-download"></i> Add to Queue
                  </button>
                </div>
              </div>
            </div>
          </div>
        `;
      });
      html += '</div>';
      
      resultsDiv.innerHTML = html;
      
    } catch (error) {
      console.error('Search error:', error);
      loadingDiv.style.display = 'none';
      errorDiv.textContent = 'Network error: ' + error.message;
      errorDiv.style.display = 'block';
    }
  }

  /**
   * Escape HTML special characters to prevent XSS.
   */
  function escapeHtml(text) {
    const map = {
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#039;'
    };
    return text.replace(/[&<>"']/g, m => map[m]);
  }

  // Ensure artist page section order matches desired flow.
  document.addEventListener('DOMContentLoaded', function() {
    const similarSection = document.getElementById('artist-similar-section');
    const topTracksSection = document.getElementById('artist-top-tracks-section');
    const genresSection = document.getElementById('artist-genres-section');
    const albumsSection = document.getElementById('albums-section');

    if (!similarSection || !topTracksSection || !genresSection || !albumsSection) {
      return;
    }

    const parent = similarSection.parentNode;
    if (!parent) {
      return;
    }

    // Place Top Tracks directly after Similar
    parent.insertBefore(topTracksSection, similarSection.nextSibling);

    // Place Genres after Top Tracks
    parent.insertBefore(genresSection, topTracksSection.nextSibling);
  });
</script>

{% endblock %}
