/* Shared camera-only subscriber. A run owns its room, listeners and server session. */
(function(root,factory){
  const api=factory();
  if(typeof module==="object"&&module.exports)module.exports=api;
  else root.CameraLiveView=api;
})(typeof globalThis!=="undefined"?globalThis:this,function(){
  "use strict";
  class Viewer {
    constructor({sdk,video,startSession,stopSession,onState=()=>{},log=(event,data)=>console.info("[LIVE_VIEW] "+event,data),timeoutMs=45000}) {
      Object.assign(this,{sdk,video,startSession,stopSession,onState,log,timeoutMs});
      this.current=null;
      this.state="stopped";
    }
    get active(){return this.current!==null;}
    owns(run){return this.current===run&&!run.closed;}
    diagnostic(run,event,extra={}){
      this.log(event,{camera_id:run.target.camera_id,edge_id:run.target.edge_id,
        session_id:run.session?.session_id,room:run.session?.room||run.room?.name,...extra});
    }
    stateFor(run,state,detail={}){
      if(!this.owns(run))return;
      this.state=state;
      this.onState(state,detail);
    }
    armTimeout(run){
      if(run.timer||!this.owns(run))return;
      run.timer=setTimeout(()=>{
        this.diagnostic(run,"startup timeout",{state:this.state});
        this.fail(run,"Timed out waiting for camera video. Check the edge connection and try again.");
      },this.timeoutMs);
    }
    clearTimer(run){clearTimeout(run.timer);run.timer=null;}
    detach(run){
      if(run.track){run.track.detach(this.video);run.track=null;}
      this.video.srcObject=null;
      this.video.style.display="none";
      this.video.controls=false;
    }
    releaseSession(run){
      if(!run.session||run.released)return Promise.resolve();
      run.released=true;
      return Promise.resolve().then(()=>this.stopSession(run.session,run.target)).catch(()=>{
        this.diagnostic(run,"stop request failed"); // TTL remains the server-side safety bound.
      });
    }
    cleanup(run){
      if(run.closed)return run.cleanupPromise||Promise.resolve();
      run.closed=true;
      this.clearTimer(run);
      run.listeners.forEach(([event,handler])=>run.room.off(event,handler));
      run.listeners=[];
      this.video.removeEventListener("playing",run.playing);
      this.detach(run); // Synchronous: an old disconnect promise must never clear a new video.
      run.cleanupPromise=Promise.allSettled([
        Promise.resolve().then(()=>run.room?.disconnect()),this.releaseSession(run)
      ]);
      return run.cleanupPromise;
    }
    stop(){
      const run=this.current;
      if(!run)return Promise.resolve();
      this.stateFor(run,"stopped");
      this.current=null;
      this.diagnostic(run,"stopped");
      return this.cleanup(run);
    }
    fail(run,message){
      if(!this.owns(run))return;
      this.diagnostic(run,"error",{reason:message});
      this.stateFor(run,"error",{message});
      this.current=null;
      void this.cleanup(run);
    }
    play(run){
      const track=run.track;
      Promise.resolve().then(()=>{
        if(this.owns(run)&&run.track===track)return this.video.play();
      }).catch(()=>{
        if(!this.owns(run)||run.track!==track)return;
        this.clearTimer(run);
        this.video.controls=true;
        this.diagnostic(run,"playback interaction required");
        this.stateFor(run,"waiting_for_video",{message:"Press play on the video to begin.",autoplayBlocked:true});
      });
    }
    reconcile(run){
      if(!this.owns(run)||run.reconnecting)return;
      const edges=[...run.room.remoteParticipants.values()].filter(p=>p.identity.startsWith("edge-"));
      const candidates=[];
      for(const participant of edges){
        for(const publication of participant.trackPublications.values()){
          if(publication.kind===this.sdk.Track.Kind.Video)candidates.push({participant,publication});
        }
      }
      candidates.sort((a,b)=>Number(b.publication.source===this.sdk.Track.Source.Camera)-Number(a.publication.source===this.sdk.Track.Source.Camera)
        ||a.participant.identity.localeCompare(b.participant.identity)||a.publication.trackSid.localeCompare(b.publication.trackSid));
      const selected=candidates[0],publication=selected?.publication;
      const track=publication?.isSubscribed?publication.track:null;
      if(run.track&&run.track!==track)this.detach(run);
      if(!track){
        this.stateFor(run,edges.length?"waiting_for_video":"waiting_for_edge");
        this.armTimeout(run);
        // autoSubscribe already requests normal subscriptions. Override only an
        // explicitly undesired publication, once per publication object.
        if(publication?.isDesired===false&&!run.requested.has(publication)){
          run.requested.add(publication);
          publication.setSubscribed(true);
        }
        return;
      }
      if(run.track===track)return;
      this.detach(run);
      run.track=track;
      this.video.autoplay=true;this.video.playsInline=true;this.video.muted=true;
      this.video.style.display="block"; // adaptiveStream must observe a visible element.
      this.stateFor(run,"waiting_for_video");
      this.armTimeout(run);
      track.attach(this.video);
      this.diagnostic(run,"video attached",this.trackDetails(publication,selected.participant,track));
      this.play(run);
    }
    trackDetails(publication,participant,track){
      return {participant:participant?.identity,publication_sid:publication?.trackSid,
        track_sid:track?.sid,kind:publication?.kind,source:publication?.source,subscribed:publication?.isSubscribed};
    }
    async start(target){
      const previous=this.current;
      if(previous){this.current=null;this.clearTimer(previous);void this.cleanup(previous);}
      const run={target,listeners:[],requested:new WeakSet(),closed:false};
      this.current=run;
      this.stateFor(run,"starting");
      this.diagnostic(run,"start requested");
      this.armTimeout(run);
      try{
        if(!this.sdk||!this.video)throw new Error("missing dependencies");
        // Keep the response even if Stop occurs during HTTP: it contains the ID
        // needed to cancel exactly this server session, never a subsequent one.
        run.session=await this.startSession(target);
        if(!this.owns(run)){await this.releaseSession(run);return;}
        this.diagnostic(run,"session received",{command_id:run.session.command_id});
        run.room=new this.sdk.Room({adaptiveStream:true,dynacast:true});
        const on=(name,handler)=>{
          const event=this.sdk.RoomEvent[name];
          const guarded=(...args)=>{
            if(!this.owns(run))return;
            try{handler(...args);}catch(_){this.fail(run,"Unable to attach camera video. Please try again.");}
          };
          run.room.on(event,guarded);run.listeners.push([event,guarded]);
        };
        run.playing=()=>{
          if(!this.owns(run)||!run.track||run.reconnecting||this.video.readyState<2||!this.video.videoWidth)return;
          if(this.state!=="playing")this.diagnostic(run,"first playable event",{track_sid:run.track.sid});
          this.clearTimer(run);this.video.controls=false;
          this.stateFor(run,"playing");
        };
        this.video.addEventListener("playing",run.playing);
        for(const [name,label] of [["ParticipantConnected","participant connected"],["ParticipantDisconnected","participant disconnected"]]){
          on(name,participant=>{this.diagnostic(run,label,{participant:participant.identity});this.reconcile(run);});
        }
        for(const [name,label] of [["TrackPublished","track published"],["TrackUnpublished","track unpublished"]]){
          on(name,(publication,participant)=>{this.diagnostic(run,label,this.trackDetails(publication,participant));this.reconcile(run);});
        }
        on("TrackSubscribed",(track,publication,participant)=>{
          this.diagnostic(run,"track subscribed",this.trackDetails(publication,participant,track));this.reconcile(run);
        });
        on("TrackUnsubscribed",(track,publication,participant)=>{
          this.diagnostic(run,"track unsubscribed",this.trackDetails(publication,participant,track));
          if(run.track===track)this.detach(run);
          this.reconcile(run);
        });
        const reconnecting=()=>{
          run.reconnecting=true;this.diagnostic(run,"reconnecting");
          this.stateFor(run,"reconnecting");this.armTimeout(run);
        };
        on("Reconnecting",reconnecting);
        on("SignalReconnecting",reconnecting);
        on("Reconnected",()=>{
          run.reconnecting=false;this.diagnostic(run,"reconnected");
          // Reattach even if the SDK retained the same track through reconnect.
          this.detach(run);this.reconcile(run);
        });
        on("ConnectionStateChanged",state=>this.diagnostic(run,"connection state changed",{connection_state:state}));
        on("Disconnected",()=>{
          this.diagnostic(run,"disconnected");this.fail(run,"Live video disconnected. Please start live view again.");
        });
        on("TrackSubscriptionFailed",sid=>this.diagnostic(run,"subscription failed",{publication_sid:sid}));
        this.diagnostic(run,"connecting");
        await run.room.connect(run.session.url,run.session.viewer_token,{autoSubscribe:true});
        if(!this.owns(run)){await run.room.disconnect();return;}
        this.diagnostic(run,"connected");
        this.reconcile(run);
      }catch(_){
        // SDK/network exceptions may contain a URL or JWT. Never log them.
        this.fail(run,"Unable to start live video. Check the connection and try again.");
      }
    }
  }
  return {Viewer};
});


